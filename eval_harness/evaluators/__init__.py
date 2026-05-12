from __future__ import annotations

from eval_harness.core.llm_backends import llm_backend_registry
from eval_harness.evaluators._embedders import embedder_registry
from eval_harness.evaluators.base import Evaluator
from eval_harness.factories import evaluator_factory

evaluator_factory.load_entry_points()
llm_backend_registry.load_entry_points()
embedder_registry.load_entry_points()

__all__ = ["Evaluator"]
