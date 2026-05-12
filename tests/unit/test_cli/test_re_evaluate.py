from __future__ import annotations

import asyncio
import json
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from click.testing import CliRunner

from eval_harness.adapters.trace.local_files_store import LocalFilesStore
from eval_harness.cli.main import cli
from eval_harness.core.models import RunSummary, Trace, TraceOutput

_STUB_MODULE = "_evalh_re_evaluate_stub_agent"
_NOW = datetime(2026, 5, 12, tzinfo=UTC)


def _install_stub() -> None:
    def agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {"final_answer": f"answer for {case['id']}"}

    mod = types.ModuleType(_STUB_MODULE)
    mod.run = agent  # type: ignore[attr-defined]
    sys.modules[_STUB_MODULE] = mod


def _make_trace(case_id: str) -> Trace:
    return Trace(
        run_id="r1",
        case_id=case_id,
        variant_name="stub",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=10,
        input={"q": case_id},
        output=TraceOutput(final_answer=f"answer for {case_id}"),
    )


async def _async_seed(tmp_path: Path) -> Path:
    cases_path = tmp_path / "cases.yaml"
    cases_path.write_text(
        """
schema_version: "1.0"
dataset:
  name: rd
cases:
  - id: c1
    input: {q: 1}
  - id: c2
    input: {q: 2}
"""
    )

    config = {
        "schema_version": "1.0",
        "eval": {"name": "demo"},
        "dataset": {"type": "yaml", "path": str(cases_path)},
        "systems": [
            {
                "name": "stub",
                "adapter": "python_function",
                "target": f"{_STUB_MODULE}:run",
            }
        ],
        "evaluators": [
            {
                "name": "mentions_answer",
                "type": "contains_text",
                "config": {"any_of": ["answer for c1", "answer for c2"]},
            }
        ],
        "run": {"max_concurrency": 1},
        "output": [{"type": "local_files", "path": str(tmp_path / "runs")}],
    }
    run_dir = tmp_path / "runs" / "r1"
    run_dir.mkdir(parents=True)

    store = LocalFilesStore(path=str(tmp_path / "runs"))
    store.rendered_config = config
    await store.open("r1", run_dir)
    for case_id in ("c1", "c2"):
        await store.save_trace(_make_trace(case_id))
    (run_dir / "results.jsonl").write_text("")
    summary = RunSummary(
        run_id="r1",
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="x",
        cases_total=2,
        variants=[],
        by_evaluator=[],
    )
    (run_dir / "summary.yaml").write_text(
        yaml.safe_dump(summary.model_dump(mode="json"))
    )
    await store.close()
    return run_dir


def _seed(tmp_path: Path) -> Path:
    _install_stub()
    return asyncio.new_event_loop().run_until_complete(_async_seed(tmp_path))


def test_re_evaluate_help_lists_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "re-evaluate" in result.output


def test_re_evaluate_appends_results_and_passes(tmp_path: Path) -> None:
    run_dir = _seed(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["re-evaluate", str(run_dir)])
    assert result.exit_code == 0, result.output

    lines = (run_dir / "results.jsonl").read_text().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert {p["case_id"] for p in parsed} == {"c1", "c2"}
    assert all(p["evaluator"] == "mentions_answer" for p in parsed)
    assert all(p["passed"] is True for p in parsed)


def test_re_evaluate_is_idempotent_for_deterministic_evaluators(
    tmp_path: Path,
) -> None:
    run_dir = _seed(tmp_path)
    runner = CliRunner()

    result1 = runner.invoke(cli, ["re-evaluate", str(run_dir)])
    assert result1.exit_code == 0, result1.output
    first = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
    ]

    result2 = runner.invoke(cli, ["re-evaluate", str(run_dir)])
    assert result2.exit_code == 0, result2.output
    all_lines = [
        json.loads(line)
        for line in (run_dir / "results.jsonl").read_text().splitlines()
    ]
    second = all_lines[len(first) :]

    assert len(second) == len(first)

    def _stable(r: dict[str, Any]) -> tuple[Any, ...]:
        return (
            r["case_id"],
            r["variant_name"],
            r["evaluator"],
            r["passed"],
            r["score"],
            r["reason"],
        )

    assert sorted(_stable(r) for r in second) == sorted(_stable(r) for r in first)


def test_re_evaluate_add_filters_to_named_evaluator(tmp_path: Path) -> None:
    run_dir = _seed(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["re-evaluate", str(run_dir), "--add", "mentions_answer"]
    )
    assert result.exit_code == 0, result.output
    lines = (run_dir / "results.jsonl").read_text().splitlines()
    assert all(json.loads(line)["evaluator"] == "mentions_answer" for line in lines)


def test_re_evaluate_add_unknown_evaluator_clean_error(tmp_path: Path) -> None:
    run_dir = _seed(tmp_path)

    runner = CliRunner()
    result = runner.invoke(
        cli, ["re-evaluate", str(run_dir), "--add", "no_such_eval"]
    )
    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "no_such_eval" in result.output
