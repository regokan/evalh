from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import yaml
from click.testing import CliRunner

from eval_harness.adapters.trace.local_files_store import LocalFilesStore
from eval_harness.cli.main import cli
from eval_harness.core.models import (
    EvaluationResult,
    EvaluatorRollup,
    EvaluatorVariantRollup,
    RunSummary,
    Trace,
    TraceOutput,
    VariantSummary,
)

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


def _trace(case_id: str, variant: str) -> Trace:
    return Trace(
        run_id="r",
        case_id=case_id,
        variant_name=variant,
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=10,
        input={"q": case_id},
        output=TraceOutput(final_answer="ok"),
    )


def _result(case_id: str, variant: str, evaluator: str, passed: bool) -> EvaluationResult:
    return EvaluationResult(
        run_id="r",
        case_id=case_id,
        variant_name=variant,
        evaluator=evaluator,
        evaluator_type="contains_text",
        passed=passed,
        reason="ok" if passed else "miss",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
    )


async def _seed_run(
    base: Path,
    run_id: str,
    *,
    variant_pass_rates: dict[str, float],
    case_results: list[tuple[str, str, str, bool]],
    evaluator_rollup: dict[str, dict[str, float]] | None = None,
) -> Path:
    """case_results: (case_id, variant, evaluator, passed)."""
    run_dir = base / "runs" / run_id
    run_dir.mkdir(parents=True)

    store = LocalFilesStore(path=str(base / "runs"))
    await store.open(run_id, run_dir)
    cases_seen: set[tuple[str, str]] = set()
    for case_id, variant, evaluator, passed in case_results:
        cases_seen.add((case_id, variant))
        await store.save_trace(_trace(case_id, variant)) if (case_id, variant) not in cases_seen else None
        await store.save_evaluation(case_id, variant, [_result(case_id, variant, evaluator, passed)])
    # Save one trace per (case, variant) pair (idempotent INSERT not needed; the
    # local files store appends so we deduped above via the set check).

    variants = [
        VariantSummary(
            name=name,
            cases_total=10,
            cases_passed=int(rate * 10),
            cases_errored=0,
            pass_rate=rate,
            avg_latency_ms=100.0,
            avg_cost_usd=None,
            avg_tokens_input=None,
            avg_tokens_output=None,
        )
        for name, rate in variant_pass_rates.items()
    ]
    by_evaluator = []
    if evaluator_rollup is not None:
        for ev_name, by_variant in evaluator_rollup.items():
            by_evaluator.append(
                EvaluatorRollup(
                    evaluator=ev_name,
                    by_variant={
                        v: EvaluatorVariantRollup(pass_rate=rate, avg_score=None)
                        for v, rate in by_variant.items()
                    },
                )
            )

    summary = RunSummary(
        run_id=run_id,
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="x",
        cases_total=10,
        variants=variants,
        by_evaluator=by_evaluator,
    )
    (run_dir / "summary.yaml").write_text(yaml.safe_dump(summary.model_dump(mode="json")))
    (run_dir / "config.yaml").write_text(yaml.safe_dump({"eval": {"name": run_id}}))
    await store.close()
    return run_dir


def _seed(base: Path, **kwargs: object) -> Path:
    return asyncio.new_event_loop().run_until_complete(_seed_run(base, **kwargs))  # type: ignore[arg-type]


def test_compare_help_lists_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "compare" in result.output


def test_compare_emits_regressions_and_improvements(tmp_path: Path) -> None:
    run_a = _seed(
        tmp_path / "a",
        run_id="ra",
        variant_pass_rates={"v1": 1.0},
        case_results=[
            ("c1", "v1", "ev", True),
            ("c2", "v1", "ev", False),
            ("c3", "v1", "ev", True),
        ],
        evaluator_rollup={"ev": {"v1": 0.66}},
    )
    run_b = _seed(
        tmp_path / "b",
        run_id="rb",
        variant_pass_rates={"v1": 0.5},
        case_results=[
            ("c1", "v1", "ev", False),  # regression
            ("c2", "v1", "ev", True),   # improvement
            ("c3", "v1", "ev", True),   # unchanged
        ],
        evaluator_rollup={"ev": {"v1": 0.66}},
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["compare", str(run_a), str(run_b)])
    assert result.exit_code == 0, result.output

    assert "Regressions" in result.output
    assert "Improvements" in result.output
    assert "c1" in result.output
    assert "c2" in result.output

    # Per-variant delta: B is 50%, A was 100% -> delta -50%.
    assert "-50" in result.output or "−50" in result.output


def test_compare_reports_cases_only_in_one_run(tmp_path: Path) -> None:
    run_a = _seed(
        tmp_path / "a",
        run_id="ra",
        variant_pass_rates={"v1": 1.0},
        case_results=[
            ("c1", "v1", "ev", True),
            ("c_a_only", "v1", "ev", True),
        ],
    )
    run_b = _seed(
        tmp_path / "b",
        run_id="rb",
        variant_pass_rates={"v1": 1.0},
        case_results=[
            ("c1", "v1", "ev", True),
            ("c_b_only", "v1", "ev", True),
        ],
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["compare", str(run_a), str(run_b)])
    assert result.exit_code == 0, result.output

    assert "c_a_only" in result.output
    assert "c_b_only" in result.output


def test_compare_missing_run_dir_yields_clean_error(tmp_path: Path) -> None:
    real = _seed(
        tmp_path,
        run_id="ra",
        variant_pass_rates={"v1": 1.0},
        case_results=[("c1", "v1", "ev", True)],
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["compare", str(real), str(tmp_path / "nope")])
    assert result.exit_code != 0
    assert "Traceback" not in result.output


def test_compare_always_exit_zero_even_with_regressions(tmp_path: Path) -> None:
    run_a = _seed(
        tmp_path / "a",
        run_id="ra",
        variant_pass_rates={"v1": 1.0},
        case_results=[("c1", "v1", "ev", True)],
    )
    run_b = _seed(
        tmp_path / "b",
        run_id="rb",
        variant_pass_rates={"v1": 0.0},
        case_results=[("c1", "v1", "ev", False)],
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["compare", str(run_a), str(run_b)])
    assert result.exit_code == 0, result.output
