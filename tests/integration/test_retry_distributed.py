"""Retry-distributed verification.

In a distributed run a worker can crash before persisting its Trace.
The resulting run dir has NO entry at all for that (case_id,
variant_name) — not even a ``Trace.error`` row — so the retry path can't
rely on scanning ``traces.jsonl`` for ``error is not None``.

This test pins the cross-executor contract:

1. A fixture run dir is seeded so cell ``c1`` has a successful trace
   and cell ``c2`` has nothing — simulating a worker that crashed
   before ``save_trace`` fired.
2. ``evalh run --retry-only-failed <run_dir>`` is invoked against an
   ``eval.yaml`` whose dataset still names both cases.
3. The retry should detect the missing cell via the plan's expected
   ``(case, variant)`` set, re-run only ``c2``, and append its trace to
   the existing ``traces.jsonl``.

The seam under test is in ``cli/commands/run.py`` —
``_collect_failed_cells`` was widened to take the plan and surface
expected-but-missing cells, not just persisted error traces.
"""

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
    TraceOutput,
    VariantSummary,
)

_STUB_MODULE = "_evalh_retry_distributed_stub"
_RUN_ID = "2026-05-12T10-00-00_retry_distributed"
_NOW = datetime(2026, 5, 12, 10, 0, 0, tzinfo=UTC)


def _install_passing_stub() -> None:
    """A stub agent that always succeeds — used when the retry path
    re-executes the missing cell."""

    def agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {
            "final_answer": f"retried-{case['id']}",
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
  name: retry_distributed
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
  name: retry_distributed
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
      all_of: ["retried-"]
run:
  max_concurrency: 2
output:
  - type: local_files
    path: {runs_dir.as_posix()}
"""
    )
    return eval_yaml


def _trace(case_id: str) -> Trace:
    return Trace(
        run_id=_RUN_ID,
        case_id=case_id,
        variant_name="stub",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=5,
        input={"q": case_id},
        output=TraceOutput(final_answer=f"ok-{case_id}"),
    )


def _result(case_id: str) -> EvaluationResult:
    return EvaluationResult(
        run_id=_RUN_ID,
        case_id=case_id,
        variant_name="stub",
        evaluator="not_empty",
        evaluator_type="contains_text",
        passed=True,
        reason="ok",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
    )


def _seed_distributed_crash(runs_dir: Path) -> Path:
    """Seed a run dir matching the worker-crashed-mid-cell shape: c1
    succeeded (Trace + result on disk), c2 was lost in transit (no Trace,
    no result, but the run's summary still reports both as expected)."""
    run_dir = runs_dir / _RUN_ID
    run_dir.mkdir(parents=True, exist_ok=True)

    (run_dir / "traces.jsonl").write_text(_trace("c1").model_dump_json() + "\n")
    (run_dir / "results.jsonl").write_text(_result("c1").model_dump_json() + "\n")

    summary = RunSummary(
        run_id=_RUN_ID,
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="seed",
        cases_total=2,
        variants=[
            VariantSummary(
                name="stub",
                cases_total=2,
                cases_passed=1,
                cases_errored=0,
                pass_rate=0.5,
                avg_latency_ms=5.0,
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
    (run_dir / "config.yaml").write_text("eval:\n  name: retry_distributed\n")
    return run_dir


def _trace_case_ids(run_dir: Path) -> list[str]:
    text = (run_dir / "traces.jsonl").read_text()
    return [json.loads(line)["case_id"] for line in text.splitlines() if line.strip()]


def test_missing_cell_with_no_trace_is_retried(tmp_path: Path) -> None:
    """Headline contract: a cell with no Trace at all (worker crashed
    before persistence) is picked up by ``--retry-only-failed`` via the
    plan's expected (case, variant) set, even though ``traces.jsonl``
    has no record of the failure."""
    _install_passing_stub()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    seeded = _seed_distributed_crash(runs_dir)
    eval_yaml = _write_eval_yaml(tmp_path, runs_dir)

    # Sanity check: nothing in the seeded traces.jsonl marks c2 as
    # failed (it just isn't there). The pre-v2 retry path would have
    # treated the run as "complete with one passing cell" and refused
    # to re-run anything.
    assert _trace_case_ids(seeded) == ["c1"]

    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", str(eval_yaml), "--retry-only-failed", str(seeded)]
    )
    assert result.exit_code == 0, result.output

    case_ids = _trace_case_ids(seeded)
    assert case_ids == ["c1", "c2"]


def test_missing_cell_and_errored_cell_both_retried(tmp_path: Path) -> None:
    """Mixed scenario: a distributed run that ends up with one passed
    cell on disk, one error-trace on disk (system failure), and one
    cell missing entirely (worker crash). All three failure modes need
    to be visible to the retry path."""
    _install_passing_stub()
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()

    cases_yaml = tmp_path / "cases.yaml"
    cases_yaml.write_text(
        """
schema_version: "1.0"
dataset:
  name: retry_mixed
cases:
  - id: c1
    input: {q: 1}
  - id: c2
    input: {q: 2}
  - id: c3
    input: {q: 3}
"""
    )
    eval_yaml = tmp_path / "eval.yaml"
    eval_yaml.write_text(
        f"""
eval:
  name: retry_mixed
dataset:
  type: yaml
  path: {cases_yaml.as_posix()}
systems:
  - name: stub
    adapter: python_function
    target: {_STUB_MODULE}:run
evaluators:
  - name: not_empty
    type: contains_text
    config:
      all_of: ["retried-"]
run:
  max_concurrency: 2
output:
  - type: local_files
    path: {runs_dir.as_posix()}
"""
    )

    seeded = runs_dir / _RUN_ID
    seeded.mkdir(parents=True, exist_ok=True)

    from eval_harness.core.models import TraceError

    err_trace = Trace(
        run_id=_RUN_ID,
        case_id="c2",
        variant_name="stub",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=5,
        input={"q": "c2"},
        output=TraceOutput(final_answer=None),
        error=TraceError(type="adapter_error", message="boom"),
    )
    (seeded / "traces.jsonl").write_text(
        _trace("c1").model_dump_json() + "\n" + err_trace.model_dump_json() + "\n"
    )
    (seeded / "results.jsonl").write_text(_result("c1").model_dump_json() + "\n")
    summary = RunSummary(
        run_id=_RUN_ID,
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="seed",
        cases_total=3,
        variants=[
            VariantSummary(
                name="stub",
                cases_total=3,
                cases_passed=1,
                cases_errored=1,
                pass_rate=1 / 3,
                avg_latency_ms=5.0,
                avg_cost_usd=None,
                avg_tokens_input=None,
                avg_tokens_output=None,
            )
        ],
        by_evaluator=[],
    )
    (seeded / "summary.yaml").write_text(
        yaml.safe_dump(summary.model_dump(mode="json"))
    )
    (seeded / "config.yaml").write_text("eval:\n  name: retry_mixed\n")

    runner = CliRunner()
    result = runner.invoke(
        cli, ["run", str(eval_yaml), "--retry-only-failed", str(seeded)]
    )
    assert result.exit_code == 0, result.output

    case_ids = _trace_case_ids(seeded)
    # Original three entries (c1, c2-err) preserved; retry appended c2 and c3.
    assert case_ids[:2] == ["c1", "c2"]
    assert set(case_ids[2:]) == {"c2", "c3"}
