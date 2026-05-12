from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from eval_harness.runner.plan_builder import RunPlan


@click.command("run")
@click.argument(
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--retry-only-failed",
    "retry_run_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=None,
    help=(
        "Reuse the run_id at this existing run_dir and re-execute only the "
        "cells that failed. New traces/results are appended to the run dir."
    ),
)
@click.option(
    "--include-evaluator-failures",
    is_flag=True,
    default=False,
    help=(
        "With --retry-only-failed: also retry cells where the system ran "
        "successfully but at least one evaluator failed or errored. Default "
        "is to retry only cells whose Trace recorded an error."
    ),
)
def run(
    config_path: Path,
    retry_run_dir: Path | None,
    include_evaluator_failures: bool,
) -> None:
    """Run an eval defined by eval.yaml.

    With --retry-only-failed, the run_id and run_dir of the existing run are
    reused: new traces are appended to traces.jsonl and the summary is
    rewritten to reflect the retried cells. Use this to amend a partial or
    flaky run without re-executing the successful subset.
    """
    # Heavy imports happen here so `evalh --help` stays fast.
    from eval_harness.core.config_loader import load_config
    from eval_harness.core.errors import ConfigError

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        summary = asyncio.run(
            _main(config, config_path, retry_run_dir, include_evaluator_failures)
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        click.echo(f"error: {type(exc).__name__}: {exc}", err=True)
        sys.exit(2)

    if summary is None:
        click.echo("retry-only-failed: no failed cells; nothing to do.")
        return

    _print_summary(summary)


async def _main(
    config: object,
    config_path: Path,
    retry_run_dir: Path | None,
    include_evaluator_failures: bool,
) -> object:
    from eval_harness.runner import build_plan, run_eval

    plan = await build_plan(config, config_path)  # type: ignore[arg-type]
    if retry_run_dir is not None:
        retried = await _retarget_to_failed(
            plan, retry_run_dir, include_evaluator_failures
        )
        if retried is None:
            return None
        plan = retried
    return await run_eval(plan)


async def _retarget_to_failed(
    plan: RunPlan,
    retry_run_dir: Path,
    include_evaluator_failures: bool,
) -> RunPlan | None:
    """Rewire `plan` so it re-executes only the failed cells of an existing run.

    Returns `None` when there are no failed cells to retry.
    """
    failed = await _collect_failed_cells(retry_run_dir, include_evaluator_failures)
    if not failed:
        return None

    case_ids = {c for c, _ in failed}
    variant_names = {v for _, v in failed}

    cases = [c for c in plan.cases if c.id in case_ids]
    variants = [v for v in plan.variants if v.name in variant_names]
    if not cases or not variants:
        return None

    system_adapters = {v.name: plan.system_adapters[v.name] for v in variants}

    # Read the run_id from the existing run's summary so the appended rows
    # carry the original run_id rather than the freshly-minted one.
    from eval_harness.core.run_reader import RunReader

    summary = await RunReader(retry_run_dir).load_summary()

    return replace(
        plan,
        run_id=summary.run_id,
        run_dir=retry_run_dir,
        cases=cases,
        variants=variants,
        system_adapters=system_adapters,
        cell_filter=frozenset(failed),
    )


async def _collect_failed_cells(
    run_dir: Path,
    include_evaluator_failures: bool,
) -> set[tuple[str, str]]:
    from eval_harness.core.run_reader import RunReader

    reader = RunReader(run_dir)
    failed: set[tuple[str, str]] = set()
    async for trace in reader.iter_traces():
        if trace.error is not None:
            failed.add((trace.case_id, trace.variant_name))
    if include_evaluator_failures:
        async for result in reader.iter_results():
            if (not result.passed) or result.error is not None:
                failed.add((result.case_id, result.variant_name))
    return failed


def _print_summary(summary: object) -> None:
    from rich.console import Console
    from rich.table import Table

    from eval_harness.core.models import RunSummary

    if not isinstance(summary, RunSummary):
        click.echo(str(summary))
        return

    console = Console()
    console.print(f"[bold]run_id[/]: {summary.run_id}")
    console.print(f"[bold]cases_total[/]: {summary.cases_total}")

    table = Table(title="Per-variant summary")
    table.add_column("variant")
    table.add_column("passed", justify="right")
    table.add_column("errored", justify="right")
    table.add_column("pass_rate", justify="right")
    table.add_column("avg_latency_ms", justify="right")
    for v in summary.variants:
        table.add_row(
            v.name,
            f"{v.cases_passed}/{v.cases_total}",
            str(v.cases_errored),
            f"{v.pass_rate:.2%}",
            f"{v.avg_latency_ms:.0f}",
        )
    console.print(table)

    if summary.comparison is not None and summary.comparison.deltas:
        cmp_table = Table(title=f"Comparison vs baseline '{summary.comparison.baseline}'")
        cmp_table.add_column("variant")
        cmp_table.add_column("pass_rate_delta", justify="right")
        cmp_table.add_column("regressions", justify="right")
        cmp_table.add_column("improvements", justify="right")
        for d in summary.comparison.deltas:
            cmp_table.add_row(
                d.variant,
                f"{d.pass_rate_delta:+.2%}",
                str(len(d.regressions)),
                str(len(d.improvements)),
            )
        console.print(cmp_table)
