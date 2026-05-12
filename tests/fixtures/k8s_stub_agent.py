"""Deterministic stub agent for the K8s Jobs executor tests.

The K8s integration tests against a real cluster need an importable
module; the fake-cluster unit tests run the worker in-process via
``worker_run_cell_sync`` so this also has to resolve in the
orchestrator's Python.
"""

from __future__ import annotations

from typing import Any


def run(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    return {
        "final_answer": f"k8s-answer-for-{case['id']}",
        "metrics": {"token_input": 4, "token_output": 6},
    }
