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
    ToolCall,
    Trace,
    TraceError,
    TraceOutput,
    VariantSummary,
)

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


def _trace(
    case_id: str,
    variant: str,
    *,
    final: str = "the final answer",
    thinking: str | None = None,
    error: TraceError | None = None,
    latency_ms: int = 12,
    tool_calls: list[ToolCall] | None = None,
) -> Trace:
    return Trace(
        run_id="r1",
        case_id=case_id,
        variant_name=variant,
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=latency_ms,
        input={"user_message": f"q-{case_id}"},
        output=TraceOutput(final_answer=final, thinking=thinking),
        tool_calls=tool_calls or [],
        error=error,
    )


def _result(
    case_id: str,
    variant: str,
    evaluator: str = "ev1",
    *,
    passed: bool = True,
    score: float | None = None,
    reason: str = "ok",
) -> EvaluationResult:
    return EvaluationResult(
        run_id="r1",
        case_id=case_id,
        variant_name=variant,
        evaluator=evaluator,
        evaluator_type="contains_text",
        passed=passed,
        score=score,
        reason=reason,
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
    )


def _summary(variants: list[str]) -> RunSummary:
    vs = [
        VariantSummary(
            name=n,
            cases_total=2,
            cases_passed=1,
            cases_errored=0,
            pass_rate=0.5,
            avg_latency_ms=42.0,
            avg_cost_usd=None,
            avg_tokens_input=None,
            avg_tokens_output=None,
        )
        for n in variants
    ]
    return RunSummary(
        run_id="r1",
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="abc",
        cases_total=2,
        variants=vs,
        by_evaluator=[],
    )


def _seed_run_dir(
    run_dir: Path,
    *,
    traces: list[Trace],
    results: list[EvaluationResult],
    summary: RunSummary | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "traces.jsonl").write_text(
        "".join(t.model_dump_json() + "\n" for t in traces)
    )
    (run_dir / "results.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in results)
    )
    s = summary or _summary(sorted({t.variant_name for t in traces}))
    (run_dir / "summary.yaml").write_text(yaml.safe_dump(s.model_dump(mode="json")))
    (run_dir / "config.yaml").write_text("eval:\n  name: demo\n")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_help_works(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["inspect", "--help"])
    assert result.exit_code == 0
    assert "Inspect a finished eval run" in result.output
    assert "--case" in result.output
    assert "--variant" in result.output
    assert "--failed" in result.output


def test_no_filter_prints_summary_and_cells(runner: CliRunner, tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    _seed_run_dir(
        run_dir,
        traces=[_trace("c1", "v1"), _trace("c2", "v1")],
        results=[_result("c1", "v1", passed=True), _result("c2", "v1", passed=False)],
    )
    result = runner.invoke(cli, ["inspect", str(run_dir)])
    assert result.exit_code == 0, result.output
    assert "Per-variant summary" in result.output
    assert "Cells" in result.output
    assert "c1" in result.output
    assert "c2" in result.output


def test_case_filter_renders_full_detail(runner: CliRunner, tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    _seed_run_dir(
        run_dir,
        traces=[
            _trace(
                "c1",
                "v1",
                final="Richmond is great",
                thinking="step by step reasoning",
                tool_calls=[ToolCall(name="lookup", arguments={"q": "ABC123"})],
            ),
            _trace("c2", "v1"),
        ],
        results=[_result("c1", "v1", reason="answer mentions suburb")],
    )
    result = runner.invoke(cli, ["inspect", str(run_dir), "--case", "c1"])
    assert result.exit_code == 0, result.output
    assert "c1" in result.output
    assert "Richmond is great" in result.output
    assert "step by step reasoning" in result.output
    assert "tool_calls" in result.output
    assert "lookup" in result.output
    assert "evaluator results" in result.output
    # The unrelated c2 should not appear in detail mode.
    assert "c2" not in result.output


def test_variant_filter_narrows_cell_list(runner: CliRunner, tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    _seed_run_dir(
        run_dir,
        traces=[_trace("c1", "v1"), _trace("c1", "v2"), _trace("c2", "v2")],
        results=[],
    )
    result = runner.invoke(cli, ["inspect", str(run_dir), "--variant", "v2"])
    assert result.exit_code == 0, result.output
    assert "c1" in result.output
    assert "c2" in result.output
    # v1 cells excluded.
    assert " v1 " not in result.output


def test_failed_filter_only_shows_failures(runner: CliRunner, tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    _seed_run_dir(
        run_dir,
        traces=[_trace("c1", "v1"), _trace("c2", "v1")],
        results=[
            _result("c1", "v1", passed=True),
            _result("c2", "v1", passed=False),
        ],
    )
    result = runner.invoke(cli, ["inspect", str(run_dir), "--failed"])
    assert result.exit_code == 0, result.output
    assert "c2" in result.output
    assert "c1" not in result.output


def test_failed_filter_treats_error_as_failure(runner: CliRunner, tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    _seed_run_dir(
        run_dir,
        traces=[
            _trace("c1", "v1"),
            _trace(
                "c_err",
                "v1",
                error=TraceError(type="adapter_error", message="boom"),
            ),
        ],
        results=[_result("c1", "v1", passed=True)],
    )
    result = runner.invoke(cli, ["inspect", str(run_dir), "--failed"])
    assert result.exit_code == 0, result.output
    assert "c_err" in result.output
    assert "c1" not in result.output


def test_long_thinking_truncated_by_default(runner: CliRunner, tmp_path: Path) -> None:
    big = "x" * 5000
    run_dir = tmp_path / "r1"
    _seed_run_dir(
        run_dir,
        traces=[_trace("c1", "v1", thinking=big)],
        results=[],
    )
    result = runner.invoke(cli, ["inspect", str(run_dir), "--case", "c1"])
    assert result.exit_code == 0, result.output
    assert "truncated" in result.output
    assert "--no-truncate" in result.output


def test_no_truncate_disables_truncation(runner: CliRunner, tmp_path: Path) -> None:
    big = "x" * 5000
    run_dir = tmp_path / "r1"
    _seed_run_dir(
        run_dir,
        traces=[_trace("c1", "v1", thinking=big)],
        results=[],
    )
    result = runner.invoke(
        cli, ["inspect", str(run_dir), "--case", "c1", "--no-truncate"]
    )
    assert result.exit_code == 0, result.output
    assert "truncated" not in result.output


def test_no_traces_match_filters(runner: CliRunner, tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    _seed_run_dir(
        run_dir,
        traces=[_trace("c1", "v1")],
        results=[],
    )
    result = runner.invoke(cli, ["inspect", str(run_dir), "--case", "nonexistent"])
    assert result.exit_code == 0, result.output
    assert "no traces match" in result.output


def test_missing_run_dir_fails(runner: CliRunner, tmp_path: Path) -> None:
    result = runner.invoke(cli, ["inspect", str(tmp_path / "nope")])
    assert result.exit_code != 0
