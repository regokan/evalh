from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvaluationResult,
    RunSummary,
    Trace,
    TraceOutput,
)
from eval_harness.core.run_reader import RunReader

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


def _make_trace(case_id: str, variant: str) -> Trace:
    return Trace(
        run_id="r1",
        case_id=case_id,
        variant_name=variant,
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=10,
        input={"q": case_id},
        output=TraceOutput(final_answer=f"ans-{case_id}-{variant}"),
    )


def _make_result(case_id: str, variant: str, evaluator: str = "ev") -> EvaluationResult:
    return EvaluationResult(
        run_id="r1",
        case_id=case_id,
        variant_name=variant,
        evaluator=evaluator,
        evaluator_type="contains_text",
        passed=True,
        reason="ok",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
    )


def _make_summary() -> RunSummary:
    return RunSummary(
        run_id="r1",
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="deadbeef",
        cases_total=2,
        variants=[],
        by_evaluator=[],
    )


def _seed_run_dir(
    run_dir: Path,
    traces: list[Trace],
    results: list[EvaluationResult],
    summary: RunSummary | None = None,
    config: dict[str, object] | None = None,
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "traces.jsonl").write_text(
        "".join(t.model_dump_json() + "\n" for t in traces)
    )
    (run_dir / "results.jsonl").write_text(
        "".join(r.model_dump_json() + "\n" for r in results)
    )
    (run_dir / "summary.yaml").write_text(
        yaml.safe_dump((summary or _make_summary()).model_dump(mode="json"))
    )
    (run_dir / "config.yaml").write_text(
        yaml.safe_dump(config or {"eval": {"name": "r1"}})
    )


def test_missing_run_dir_raises_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="run directory does not exist"):
        RunReader(tmp_path / "nope")


def test_missing_required_file_raises_config_error(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    run_dir.mkdir()
    (run_dir / "config.yaml").write_text("eval: {name: r1}\n")
    (run_dir / "summary.yaml").write_text("run_id: r1\n")
    (run_dir / "traces.jsonl").write_text("")
    # results.jsonl deliberately omitted
    with pytest.raises(ConfigError, match="results.jsonl"):
        RunReader(run_dir)


async def test_iter_traces_yields_pydantic_models(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    traces = [
        _make_trace("c1", "v1"),
        _make_trace("c1", "v2"),
        _make_trace("c2", "v1"),
    ]
    _seed_run_dir(run_dir, traces=traces, results=[])

    reader = RunReader(run_dir)
    collected = [t async for t in reader.iter_traces()]

    assert len(collected) == 3
    assert all(isinstance(t, Trace) for t in collected)
    assert [t.case_id for t in collected] == ["c1", "c1", "c2"]


async def test_corrupt_jsonl_raises_config_error_with_line_number(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "r1"
    _seed_run_dir(run_dir, traces=[_make_trace("c1", "v1")], results=[])
    # Append a corrupt line.
    with (run_dir / "traces.jsonl").open("a") as f:
        f.write("{not-json\n")

    reader = RunReader(run_dir)
    with pytest.raises(ConfigError, match="traces.jsonl:2"):
        async for _ in reader.iter_traces():
            pass


async def test_get_trace_returns_none_for_missing_cell(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    _seed_run_dir(
        run_dir,
        traces=[_make_trace("c1", "v1"), _make_trace("c2", "v2")],
        results=[],
    )
    reader = RunReader(run_dir)
    assert await reader.get_trace("c1", "v1") is not None
    assert await reader.get_trace("c1", "v2") is None
    assert await reader.get_trace("c-missing", "v1") is None


async def test_get_results_filters_by_case_and_variant(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    results = [
        _make_result("c1", "v1", "a"),
        _make_result("c1", "v1", "b"),
        _make_result("c2", "v1", "a"),
        _make_result("c1", "v2", "a"),
    ]
    _seed_run_dir(run_dir, traces=[_make_trace("c1", "v1")], results=results)
    reader = RunReader(run_dir)
    matched = await reader.get_results("c1", "v1")
    assert [r.evaluator for r in matched] == ["a", "b"]


async def test_list_case_ids_and_variants_distinct_in_order(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    traces = [
        _make_trace("c2", "v1"),
        _make_trace("c1", "v1"),
        _make_trace("c2", "v2"),
        _make_trace("c1", "v2"),
    ]
    _seed_run_dir(run_dir, traces=traces, results=[])
    reader = RunReader(run_dir)
    assert await reader.list_case_ids() == ["c2", "c1"]
    assert await reader.list_variant_names() == ["v1", "v2"]


async def test_load_summary_round_trips(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    summary = _make_summary()
    _seed_run_dir(run_dir, traces=[], results=[], summary=summary)
    reader = RunReader(run_dir)
    loaded = await reader.load_summary()
    assert loaded.run_id == summary.run_id
    assert loaded.cases_total == summary.cases_total


async def test_load_config_returns_dict(tmp_path: Path) -> None:
    run_dir = tmp_path / "r1"
    _seed_run_dir(
        run_dir,
        traces=[],
        results=[],
        config={"eval": {"name": "demo"}, "providers": {"OPENAI_API_KEY": "***MASKED***"}},
    )
    reader = RunReader(run_dir)
    cfg = await reader.load_config()
    assert cfg["eval"]["name"] == "demo"
    assert cfg["providers"]["OPENAI_API_KEY"] == "***MASKED***"
