from __future__ import annotations

from eval_harness.evaluators._judge_backends import judge_backend_registry
from eval_harness.evaluators.base import Evaluator
from eval_harness.factories import evaluator_factory

evaluator_factory.load_entry_points()
judge_backend_registry.load_entry_points()

__all__ = ["Evaluator"]
