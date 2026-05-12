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

_STUB_GOOD = "_evalh_variant_cmp_good_agent"
_STUB_BAD = "_evalh_variant_cmp_bad_agent"


def _install_stubs() -> None:
    def good(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {
            "final_answer": "the answer mentions Richmond and Brunswick and Carlton",
        }

    def bad(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {"final_answer": "no match here"}

    for name, fn in [(_STUB_GOOD, good), (_STUB_BAD, bad)]:
        mod = types.ModuleType(name)
        mod.run = fn  # type: ignore[attr-defined]
        sys.modules[name] = mod


def _write_cases(tmp_path: Path) -> Path:
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        """
schema_version: "1.0"
dataset:
  name: comp
cases:
  - id: c1
    input: {q: 1}
  - id: c2
    input: {q: 2}
  - id: c3
    input: {q: 3}
"""
    )
    return cases


def _build_config(tmp_path: Path, cases_path: Path) -> EvalConfig:
    return EvalConfig(
        eval=EvalIdentity(name="variant_compare"),
        dataset=DatasetConfig(type="yaml", path=str(cases_path)),
        systems=[
            SystemConfig.model_validate(
                {
                    "name": "baseline",
                    "adapter": "python_function",
                    "target": f"{_STUB_GOOD}:run",
                }
            ),
            SystemConfig.model_validate(
                {
                    "name": "candidate",
                    "adapter": "python_function",
                    "target": f"{_STUB_BAD}:run",
                }
            ),
        ],
        evaluators=[
            EvaluatorConfig(
                name="mentions",
                type="contains_text",
                config={"any_of": ["Richmond", "Brunswick", "Carlton"]},
            )
        ],
        run=RunOptions(max_concurrency=4, baseline_variant="baseline"),
        output=[OutputConfig(type="local_files", path=str(tmp_path / "runs"))],
    )


async def test_baseline_comparison_emits_per_case_deltas(tmp_path: Path) -> None:
    _install_stubs()
    cases_path = _write_cases(tmp_path)
    config = _build_config(tmp_path, cases_path)

    plan = await build_plan(config, tmp_path / "eval.yaml")
    summary = await run_eval(plan)

    assert summary.comparison is not None
    assert summary.comparison.baseline == "baseline"
    assert len(summary.comparison.deltas) == 1
    delta = summary.comparison.deltas[0]
    assert delta.variant == "candidate"
    # Baseline passes all 3; candidate fails all 3 -> 3 regressions, 0 improvements.
    assert sorted(delta.regressions) == ["c1", "c2", "c3"]
    assert delta.improvements == []
    assert delta.pass_rate_delta < 0
