from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from click.testing import CliRunner

from eval_harness.cli.main import cli
from eval_harness.core.models import (
    EvaluationResult,
    RunSummary,
    Trace,
    TraceError,
    TraceOutput,
    VariantSummary,
)

_STUB_MODULE = "_evalh_retry_stub_agent"
_RUN_ID = "2026-05-12T10-00-00_retry_smoke"
_NOW = datetime(2026, 5, 12, 10, 0, 0, tzinfo=UTC)


def _install_passing_stub() -> None:
    """Stub agent that always succeeds. Used for the retried-cell re-execution."""

    def agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {
            "final_answer": f"retry-answer-for-{case['id']}",
            "metrics": {"token_input": 1, "token_output": 1, "cost_usd": 0.0},
        }

    mod = types.ModuleType(_STUB_MODULE)
    mod.run = agent  # type: ignore[attr-defined]
    sys.modules[_STUB_MODULE] = mod


def _write_eval_yaml(tmp_path: Path, runs_dir: Path) -> Path:
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        """
schema_version: "1.0"
dataset:
  name: retry
cases:
  - id: c1
    input: {q: 1}
  - id: c2
    input: {q: 2}
"""
    )
    eval_yaml = tmp_path / "eval.yaml"
    eval_yaml.write_text(
        f"""
eval:
  name: retry_smoke
dataset:
  type: yaml
  path: {cases.as_posix()}
systems:
  - name: stub
    adapter: python_function
    target: {_STUB_MODULE}:run
evaluators:
  - name: not_empty
    type: contains_text
    config:
      all_of: ["retry-answer"]
run:
  max_concurrency: 2
output:
  - type: local_files
    path: {runs_dir.as_posix()}
"""
    )
    return eval_yaml


def _seed_run_dir(
    runs_dir: Path,
    *,
    traces: list[Trace],
    results: list[EvaluationResult],
) -> Path:
    run_dir = runs_dir / _RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "traces.jsonl").write_text(
        "".join(t.model_dump_json() + "\n" for t in traces)
    )
    (run_dir / "results.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in results)
    )
    summary = RunSummary(
        run_id=_RUN_ID,
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="seed",
        cases_total=len({t.case_id for t in traces}),
        variants=[
            VariantSummary(
                name="stub",
                cases_total=len({t.case_id for t in traces}),
                cases_passed=sum(1 for t in traces if t.error is None),
                cases_errored=sum(1 for t in traces if t.error is not None),
                pass_rate=0.0,
                avg_latency_ms=10.0,
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
    (run_dir / "config.yaml").write_text("eval:\n  name: retry_smoke\n")
    return run_dir


def _trace(case_id: str, *, error: bool = False, final: str = "ok") -> Trace:
    return Trace(
        run_id=_RUN_ID,
        case_id=case_id,
        variant_name="stub",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=10,
        input={"q": case_id},
        output=TraceOutput(final_answer=None if error else final),
        error=TraceError(type="adapter_error", message="boom") if error else None,
    )


def _result(case_id: str, *, passed: bool) -> EvaluationResult:
    return EvaluationResult(
        run_id=_RUN_ID,
        case_id=case_id,
        variant_name="stub",
        evaluator="not_empty",
        evaluator_type="contains_text",
        passed=passed,
        reason="ok" if passed else "missing",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
    )


def _trace_case_ids(run_dir: Path) -> list[str]:
    lines = [
        line
        for line in (run_dir / "traces.jsonl").read_text().splitlines()
        if line.strip()
    ]
    return [json.loads(line)["case_id"] for line in lines]


def test_retry_only_failed_runs_only_errored_cells(tmp_path: Path) -> None:
    _install_passing_stub()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    seeded = _seed_run_dir(
        runs_dir,
        traces=[_trace("c1"), _trace("c2", error=True)],
        results=[_result("c1", passed=True)],
    )
    eval_yaml = _write_eval_yaml(tmp_path, runs_dir)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", str(eval_yaml), "--retry-only-failed", str(seeded)]
    )
    assert result.exit_code == 0, result.output

    # No new run dir created — the existing one was reused.
    assert sorted(p.name for p in runs_dir.iterdir()) == [_RUN_ID]

    # traces.jsonl has the original 2 lines plus exactly 1 retried trace (c2).
    case_ids = _trace_case_ids(seeded)
    assert case_ids[:2] == ["c1", "c2"]  # originals untouched, order preserved
    assert len(case_ids) == 3
    assert case_ids[2] == "c2"


def test_retry_only_failed_skips_evaluator_failures_by_default(tmp_path: Path) -> None:
    _install_passing_stub()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    # System succeeded for both cells; one had an evaluator failure. With no
    # --include-evaluator-failures flag, nothing should be retried.
    seeded = _seed_run_dir(
        runs_dir,
        traces=[_trace("c1"), _trace("c2")],
        results=[_result("c1", passed=True), _result("c2", passed=False)],
    )
    eval_yaml = _write_eval_yaml(tmp_path, runs_dir)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", str(eval_yaml), "--retry-only-failed", str(seeded)]
    )
    assert result.exit_code == 0, result.output
    assert "no failed cells" in result.output

    # traces.jsonl unchanged.
    assert _trace_case_ids(seeded) == ["c1", "c2"]


def test_retry_with_include_evaluator_failures(tmp_path: Path) -> None:
    _install_passing_stub()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    seeded = _seed_run_dir(
        runs_dir,
        traces=[_trace("c1"), _trace("c2")],
        results=[_result("c1", passed=True), _result("c2", passed=False)],
    )
    eval_yaml = _write_eval_yaml(tmp_path, runs_dir)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "run",
            str(eval_yaml),
            "--retry-only-failed",
            str(seeded),
            "--include-evaluator-failures",
        ],
    )
    assert result.exit_code == 0, result.output

    case_ids = _trace_case_ids(seeded)
    assert case_ids[:2] == ["c1", "c2"]
    assert len(case_ids) == 3
    assert case_ids[2] == "c2"


def test_retry_when_nothing_failed_is_a_noop(tmp_path: Path) -> None:
    _install_passing_stub()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    seeded = _seed_run_dir(
        runs_dir,
        traces=[_trace("c1"), _trace("c2")],
        results=[_result("c1", passed=True), _result("c2", passed=True)],
    )
    eval_yaml = _write_eval_yaml(tmp_path, runs_dir)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", str(eval_yaml), "--retry-only-failed", str(seeded)]
    )
    assert result.exit_code == 0, result.output
    assert "no failed cells" in result.output
    assert _trace_case_ids(seeded) == ["c1", "c2"]


def test_retry_only_failed_help_text_documents_flag() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["run", "--help"])
    assert result.exit_code == 0
    assert "--retry-only-failed" in result.output
    assert "--include-evaluator-failures" in result.output
