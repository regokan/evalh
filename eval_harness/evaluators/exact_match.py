from __future__ import annotations

from typing import ClassVar

from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
)
from eval_harness.evaluators.base import Evaluator


class ExactMatchEvaluator(Evaluator):
    type: ClassVar[str] = "exact_match"

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        raise NotImplementedError("ExactMatchEvaluator.evaluate lands in ev-4us")
