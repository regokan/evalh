from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

from eval_harness.core.config import (
    DatasetConfig,
    EvalConfig,
    EvalIdentity,
    EvaluatorConfig,
    OutputConfig,
    RunOptions,
    SystemConfig,
)
from eval_harness.runner import build_plan, run_eval

_STUB_MODULE = "_evalh_cost_limit_stub_agent"


def _install_stub() -> None:
    """Each call returns a rising cost: 1.0, 2.0, 3.0, ..."""

    counter = {"n": 0}

    def agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        counter["n"] += 1
        return {
            "final_answer": f"answer for {case['id']}",
            "metrics": {"cost_usd": float(counter["n"])},
        }

    mod = types.ModuleType(_STUB_MODULE)
    mod.run = agent  # type: ignore[attr-defined]
    mod._counter = counter  # type: ignore[attr-defined]
    sys.modules[_STUB_MODULE] = mod


def _write_cases(tmp_path: Path) -> Path:
    cases = tmp_path / "cases.yaml"
    body = ["schema_version: \"1.0\"", "dataset:", "  name: cost", "cases:"]
    for i in range(5):
        body.extend([f"  - id: c{i}", f"    input: {{q: {i}}}"])
    cases.write_text("\n".join(body) + "\n")
    return cases


def _build_config(tmp_path: Path, cases_path: Path, *, limit: float) -> EvalConfig:
    return EvalConfig(
        eval=EvalIdentity(name="cost_test"),
        dataset=DatasetConfig(type="yaml", path=str(cases_path)),
        systems=[
            SystemConfig.model_validate(
                {
                    "name": "stub",
                    "adapter": "python_function",
                    "target": f"{_STUB_MODULE}:run",
                }
            ),
        ],
        evaluators=[
            EvaluatorConfig(
                name="contains",
                type="contains_text",
                config={"any_of": ["answer"]},
            )
        ],
        # max_concurrency=1 makes cell ordering deterministic so we can assert
        # exactly which cells short-circuit.
        run=RunOptions(max_concurrency=1, cost_limit_usd=limit),
        output=[OutputConfig(type="local_files", path=str(tmp_path / "runs"))],
    )


async def test_cost_limit_short_circuits_remaining_cells(tmp_path: Path) -> None:
    _install_stub()
    cases_path = _write_cases(tmp_path)
    # Costs are 1, 2, 3, 4, 5 across the 5 cells. Limit at 2.5: after the
    # second cell the accumulator hits 3.0 >= 2.5 and the remaining 3 cells
    # short-circuit.
    config = _build_config(tmp_path, cases_path, limit=2.5)

    plan = await build_plan(config, tmp_path / "eval.yaml")
    summary = await run_eval(plan)

    # 5 case ids → 5 outcomes total.
    assert summary.cases_total == 5

    run_dir = next((tmp_path / "runs").iterdir())
    trace_lines = (run_dir / "traces.jsonl").read_text().splitlines()
    import json as _json

    traces = [_json.loads(line) for line in trace_lines]
    error_types = [
        (t.get("error") or {}).get("type") for t in traces if t.get("error")
    ]
    successful = [t for t in traces if t.get("error") is None]

    assert len(successful) == 2, [t["case_id"] for t in traces]
    assert error_types.count("cost_limit") == 3


async def test_no_cost_limit_runs_all_cells(tmp_path: Path) -> None:
    _install_stub()
    cases_path = _write_cases(tmp_path)
    config = _build_config(tmp_path, cases_path, limit=0.0)  # 0 means "no limit" only when None
    config.run.cost_limit_usd = None  # explicit None disables guard

    plan = await build_plan(config, tmp_path / "eval.yaml")
    summary = await run_eval(plan)

    run_dir = next((tmp_path / "runs").iterdir())
    trace_lines = (run_dir / "traces.jsonl").read_text().splitlines()
    import json as _json

    traces = [_json.loads(line) for line in trace_lines]
    assert summary.cases_total == 5
    assert all(t.get("error") is None for t in traces)
