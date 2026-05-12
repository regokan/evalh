"""`evalh drift` — compare a run against its baseline.

Loads the current baseline for the run's eval_name (or an explicit
``--baseline``), computes a `ComparisonReport` with ``kind='drift'``
from the shared `_deltas.py` primitives, prints a markdown report to
stdout, and writes the structured report to
``<run_dir>/drift.yaml``.

Informational by default — `--exit-nonzero-on-regression` makes the
command return 1 when any regression case is present, so CI can gate
on it.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click
import yaml

from eval_harness.core.baseline import get_baseline_run
from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    ComparisonReport,
    EvaluationResult,
    Trace,
    VariantDelta,
)
from eval_harness.runner._deltas import (
    compute_evaluator_deltas,
    compute_improvements,
    compute_latency_cost_deltas,
    compute_pass_rate_delta,
    compute_regressions,
    pass_map,
)


@click.command("drift")
@click.argument(
    "run_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--baseline",
    "baseline_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help="Explicit baseline run dir; bypasses the promoted-symlink lookup.",
)
@click.option(
    "--exit-nonzero-on-regression",
    "exit_nonzero_on_regression",
    is_flag=True,
    default=False,
    help="Exit 1 when any regression case is present (for CI gates).",
)
def drift(
    run_dir: Path,
    baseline_dir: Path | None,
    exit_nonzero_on_regression: bool,
) -> None:
    """Compare `run_dir` against its baseline; print + save a DriftReport."""
    try:
        exit_code = asyncio.run(
            _drift(run_dir, baseline_dir, exit_nonzero_on_regression)
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    if exit_code != 0:
        sys.exit(exit_code)


async def _drift(
    run_dir: Path,
    baseline_dir: Path | None,
    exit_nonzero_on_regression: bool,
) -> int:
    from eval_harness.core.run_reader import RunReader

    current_reader = RunReader(run_dir)
    current_summary = await current_reader.load_summary()

    resolved_baseline = _resolve_baseline(run_dir, baseline_dir, current_summary.config_path)
    if resolved_baseline is None:
        click.echo("no baseline; nothing to compare")
        return 0

    baseline_reader = RunReader(resolved_baseline)
    baseline_summary = await baseline_reader.load_summary()

    current_results: list[EvaluationResult] = [
        r async for r in current_reader.iter_results()
    ]
    baseline_results: list[EvaluationResult] = [
        r async for r in baseline_reader.iter_results()
    ]

    current_traces = [t async for t in current_reader.iter_traces()]
    baseline_traces = [t async for t in baseline_reader.iter_traces()]

    report = _build_drift_report(
        baseline_run_id=baseline_summary.run_id,
        baseline_results=baseline_results,
        current_results=current_results,
        baseline_traces=baseline_traces,
        current_traces=current_traces,
    )

    _print_drift_markdown(
        report,
        baseline_path=resolved_baseline,
        current_path=run_dir,
        evaluator_deltas=compute_evaluator_deltas(baseline_results, current_results),
        latency_cost_deltas=compute_latency_cost_deltas(baseline_traces, current_traces),
    )

    drift_path = run_dir / "drift.yaml"
    drift_path.write_text(
        yaml.safe_dump(report.model_dump(mode="json"), sort_keys=False)
    )

    if exit_nonzero_on_regression and (report.regressions_count or 0) > 0:
        return 1
    return 0


def _resolve_baseline(
    run_dir: Path, baseline_override: Path | None, config_path_label: str
) -> Path | None:
    if baseline_override is not None:
        return baseline_override.resolve()

    eval_name = _eval_name_for(run_dir, config_path_label)
    if not eval_name:
        return None
    return get_baseline_run(eval_name, runs_root=run_dir.parent)


def _eval_name_for(run_dir: Path, config_path_label: str) -> str | None:
    """Read `eval.name` from `<run_dir>/config.yaml`. Falls back to the
    `summary.config_path` filename stem when config.yaml isn't on disk
    (legacy runs)."""
    config = run_dir / "config.yaml"
    if config.exists():
        try:
            data = yaml.safe_load(config.read_text()) or {}
        except yaml.YAMLError:
            data = {}
        eval_block = data.get("eval") if isinstance(data, dict) else None
        if isinstance(eval_block, dict):
            name = eval_block.get("name")
            if isinstance(name, str) and name:
                return name
    # Last resort: the run_id usually encodes the eval name as a suffix.
    return None


def _build_drift_report(
    *,
    baseline_run_id: str,
    baseline_results: list[EvaluationResult],
    current_results: list[EvaluationResult],
    baseline_traces: list[Trace],
    current_traces: list[Trace],
) -> ComparisonReport:
    """Build a ComparisonReport(kind='drift'). We model the current run
    as a single variant named ``current`` against the baseline so the
    existing `VariantDelta` shape works unchanged — drift reports are
    cross-run rather than cross-variant, but the structure is the same."""
    base_pass = pass_map(baseline_results)
    curr_pass = pass_map(current_results)

    regressions = compute_regressions(base_pass, curr_pass)
    improvements = compute_improvements(base_pass, curr_pass)
    pass_rate_delta = compute_pass_rate_delta(base_pass, curr_pass)
    latency_cost_deltas = compute_latency_cost_deltas(
        baseline_traces, current_traces
    )
    avg_latency_delta_ms = latency_cost_deltas.get("latency_ms", 0.0)

    return ComparisonReport(
        baseline=baseline_run_id,
        deltas=[
            VariantDelta(
                variant="current",
                pass_rate_delta=pass_rate_delta,
                avg_latency_delta_ms=avg_latency_delta_ms,
                regressions=regressions,
                improvements=improvements,
            )
        ],
        kind="drift",
        baseline_run_id=baseline_run_id,
        regressions_count=len(regressions),
        improvements_count=len(improvements),
    )


def _print_drift_markdown(
    report: ComparisonReport,
    *,
    baseline_path: Path,
    current_path: Path,
    evaluator_deltas: dict[str, float],
    latency_cost_deltas: dict[str, float],
) -> None:
    """Stdout markdown — terse, scriptable, the same shape webhook
    formatters render later."""
    delta = report.deltas[0]
    lines: list[str] = [
        f"# drift: {report.baseline_run_id} -> current",
        "",
        f"- baseline: `{baseline_path}`",
        f"- current:  `{current_path}`",
        f"- pass-rate delta: {delta.pass_rate_delta:+.2%}",
        f"- regressions: {report.regressions_count}",
        f"- improvements: {report.improvements_count}",
        "",
    ]
    if delta.regressions:
        lines.append("## Regressions")
        lines.extend(f"- `{cid}`" for cid in delta.regressions)
        lines.append("")
    if delta.improvements:
        lines.append("## Improvements")
        lines.extend(f"- `{cid}`" for cid in delta.improvements)
        lines.append("")
    if evaluator_deltas:
        lines.append("## Per-evaluator pass-rate delta")
        for name in sorted(evaluator_deltas):
            lines.append(f"- `{name}`: {evaluator_deltas[name]:+.2%}")
        lines.append("")
    if latency_cost_deltas:
        lines.append("## Latency / cost delta (avg)")
        for metric in sorted(latency_cost_deltas):
            lines.append(f"- `{metric}`: {latency_cost_deltas[metric]:+.4f}")
        lines.append("")
    click.echo("\n".join(lines))
