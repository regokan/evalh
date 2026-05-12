"""Executor abstraction — the v2 dispatch primitive.

The runner builds `CellDescriptor`s and hands them to an `Executor`.
Concrete executors (Local in F2; Modal / K8s / Celery / Ray later)
carry the work in their respective execution environments.

Worker code **rebuilds adapters from config + entry-points**; this
module does not pickle live adapter instances. Function-pickling looks
attractive for trivial cases and dies the moment custom-evaluator
entry-points (the v0.1 plugin path) enter the picture. See
docs/Executors.md.
"""

from __future__ import annotations

from eval_harness.core.executors.base import (
    CellHandle,
    Executor,
    ExecutorRegistry,
    executor_registry,
)

__all__ = [
    "CellHandle",
    "Executor",
    "ExecutorRegistry",
    "executor_registry",
]
