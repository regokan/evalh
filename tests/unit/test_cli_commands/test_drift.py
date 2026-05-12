"""`evalh drift` tests.

Synthesizes a baseline + current run dir under tmp_path, invokes the CLI
via Click's `CliRunner`, and asserts on stdout markdown + the on-disk
`drift.yaml`. The drift arithmetic itself is unit-tested in
`tests/unit/test_runner_deltas.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner

from eval_harness.cli.main import cli
from eval_harness.core.models import (
    EvaluationResult,
    RunSummary,
    Trace,
    TraceMetrics,
    TraceOutput,
    VariantSummary,
)

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _trace(case_id: str, *, latency_ms: int = 10) -> Trace:
    return Trace(
        run_id="r1",
        case_id=case_id,
        variant_name="v1",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=latency_ms,
        input={},
        output=TraceOutput(final_answer="x"),
        metrics=TraceMetrics(),
    )


def _result(case_id: str, *, passed: bool = True, evaluator: str = "ev1") -> EvaluationResult:
    return EvaluationResult(
        run_id="r1",
        case_id=case_id,
        variant_name="v1",
        evaluator=evaluator,
        evaluator_type="contains_text",
        passed=passed,
        score=None,
        reason="ok" if passed else "no",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
    )


def _seed_run(
    run_dir: Path,
    *,
    eval_name: str = "demo",
    traces: list[Trace],
    results: list[EvaluationResult],
    run_id: str | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.yaml").write_text(f"eval:\n  name: {eval_name}\n")
    (run_dir / "traces.jsonl").write_text(
        "".join(t.model_dump_json() + "\n" for t in traces)
    )
    (run_dir / "results.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in results)
    )
    summary = RunSummary(
        run_id=run_id or run_dir.name,
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="abc",
        cases_total=len({t.case_id for t in traces}),
        variants=[
            VariantSummary(
                name="v1",
                cases_total=len({t.case_id for t in traces}),
                cases_passed=sum(1 for r in results if r.passed),
                cases_errored=0,
                pass_rate=0.0,
                avg_latency_ms=0.0,
                avg_cost_usd=None,
                avg_tokens_input=None,
                avg_tokens_output=None,
            )
        ],
        by_evaluator=[],
    )
    (run_dir / "summary.yaml").write_text(
        yaml.safe_dump(summary.model_dump(mode="json"))
    )


def _promote(run_dir: Path) -> None:
    """Helper: drive the promote CLI to set baseline."""
    res = CliRunner().invoke(cli, ["promote", str(run_dir)])
    assert res.exit_code == 0, res.output


# ---- Help ---------------------------------------------------------------


def test_help_lists_drift(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["drift", "--help"])
    assert result.exit_code == 0
    assert "Compare" in result.output
    assert "--baseline" in result.output
    assert "--exit-nonzero-on-regression" in result.output


# ---- No baseline path --------------------------------------------------


def test_drift_no_baseline_prints_notice_and_exits_zero(
    runner: CliRunner, tmp_path: Path
) -> None:
    """No promoted baseline -> exit 0 with an explicit message. Same shape
    as `evalh compare` on a fresh repo."""
    run = tmp_path / "current"
    _seed_run(run, traces=[_trace("c1")], results=[_result("c1")])
    result = runner.invoke(cli, ["drift", str(run)])
    assert result.exit_code == 0, result.output
    assert "no baseline" in result.output
    # No drift.yaml written when there's nothing to compare.
    assert not (run / "drift.yaml").exists()


# ---- Happy path: baseline via promote ----------------------------------


def test_drift_reports_regressions_against_promoted_baseline(
    runner: CliRunner, tmp_path: Path
) -> None:
    base = tmp_path / "base"
    curr = tmp_path / "current"
    _seed_run(
        base,
        traces=[_trace("c1"), _trace("c2")],
        results=[_result("c1", passed=True), _result("c2", passed=True)],
        run_id="run-base",
    )
    _seed_run(
        curr,
        traces=[_trace("c1"), _trace("c2")],
        results=[_result("c1", passed=True), _result("c2", passed=False)],
        run_id="run-curr",
    )
    _promote(base)

    result = runner.invoke(cli, ["drift", str(curr)])
    assert result.exit_code == 0, result.output
    # Stdout markdown includes the regressing case.
    assert "drift:" in result.output
    assert "regressions: 1" in result.output
    assert "c2" in result.output
    # Persisted drift.yaml has kind='drift' + counts.
    drift_yaml = yaml.safe_load((curr / "drift.yaml").read_text())
    assert drift_yaml["kind"] == "drift"
    assert drift_yaml["regressions_count"] == 1
    assert drift_yaml["improvements_count"] == 0
    assert drift_yaml["baseline_run_id"] == "run-base"
    assert drift_yaml["deltas"][0]["regressions"] == ["c2"]


def test_drift_reports_improvements(runner: CliRunner, tmp_path: Path) -> None:
    base = tmp_path / "base"
    curr = tmp_path / "current"
    _seed_run(
        base,
        traces=[_trace("c1")],
        results=[_result("c1", passed=False)],
        run_id="run-base",
    )
    _seed_run(
        curr,
        traces=[_trace("c1")],
        results=[_result("c1", passed=True)],
        run_id="run-curr",
    )
    _promote(base)

    result = runner.invoke(cli, ["drift", str(curr)])
    assert result.exit_code == 0, result.output
    drift_yaml = yaml.safe_load((curr / "drift.yaml").read_text())
    assert drift_yaml["improvements_count"] == 1
    assert drift_yaml["regressions_count"] == 0


# ---- Explicit --baseline override --------------------------------------


def test_drift_with_explicit_baseline_bypasses_symlink_lookup(
    runner: CliRunner, tmp_path: Path
) -> None:
    base = tmp_path / "base"
    curr = tmp_path / "current"
    _seed_run(base, traces=[_trace("c1")], results=[_result("c1", passed=True)], run_id="run-base")
    _seed_run(curr, traces=[_trace("c1")], results=[_result("c1", passed=False)], run_id="run-curr")
    # No promote — explicit --baseline supplies the answer.
    result = runner.invoke(cli, ["drift", str(curr), "--baseline", str(base)])
    assert result.exit_code == 0, result.output
    drift_yaml = yaml.safe_load((curr / "drift.yaml").read_text())
    assert drift_yaml["baseline_run_id"] == "run-base"
    assert drift_yaml["regressions_count"] == 1


# ---- --exit-nonzero-on-regression --------------------------------------


def test_drift_exit_nonzero_on_regression_returns_1(
    runner: CliRunner, tmp_path: Path
) -> None:
    base = tmp_path / "base"
    curr = tmp_path / "current"
    _seed_run(base, traces=[_trace("c1")], results=[_result("c1", passed=True)], run_id="run-base")
    _seed_run(curr, traces=[_trace("c1")], results=[_result("c1", passed=False)], run_id="run-curr")
    _promote(base)

    result = runner.invoke(
        cli,
        ["drift", str(curr), "--exit-nonzero-on-regression"],
    )
    assert result.exit_code == 1, result.output
    # Even on the failure path, drift.yaml is still written so CI artifacts
    # carry the structured report.
    assert (curr / "drift.yaml").exists()


def test_drift_exit_nonzero_with_no_regressions_returns_0(
    runner: CliRunner, tmp_path: Path
) -> None:
    base = tmp_path / "base"
    curr = tmp_path / "current"
    _seed_run(base, traces=[_trace("c1")], results=[_result("c1", passed=True)], run_id="run-base")
    _seed_run(curr, traces=[_trace("c1")], results=[_result("c1", passed=True)], run_id="run-curr")
    _promote(base)
    result = runner.invoke(
        cli,
        ["drift", str(curr), "--exit-nonzero-on-regression"],
    )
    assert result.exit_code == 0, result.output


# ---- drift.yaml shape --------------------------------------------------


def test_drift_yaml_round_trips_to_comparison_report(
    runner: CliRunner, tmp_path: Path
) -> None:
    """The on-disk drift.yaml must round-trip back into a
    `ComparisonReport(kind='drift')`. Webhook formatters depend on this
    in the next bead."""
    from eval_harness.core.models import ComparisonReport

    base = tmp_path / "base"
    curr = tmp_path / "current"
    _seed_run(base, traces=[_trace("c1")], results=[_result("c1", passed=True)], run_id="run-base")
    _seed_run(curr, traces=[_trace("c1")], results=[_result("c1", passed=False)], run_id="run-curr")
    _promote(base)
    runner.invoke(cli, ["drift", str(curr)])

    loaded = yaml.safe_load((curr / "drift.yaml").read_text())
    report = ComparisonReport.model_validate(loaded)
    assert report.kind == "drift"
    assert report.baseline_run_id == "run-base"
    assert report.regressions_count == 1
