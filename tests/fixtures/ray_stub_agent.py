"""Deterministic stub agent for the Ray executor integration test.

Lives as a real importable module (not a runtime ``types.ModuleType``)
so Ray workers — which run in subprocesses without the orchestrator's
in-memory ``sys.modules`` — can resolve it via
``python_function`` adapter's ``module:func`` target. The test ships
this directory through ``runtime_env={"working_dir": ...}``.
"""

from __future__ import annotations

from typing import Any


def run(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    return {
        "final_answer": f"ray-answer-for-{case['id']}",
        "metrics": {"token_input": 5, "token_output": 7},
    }
