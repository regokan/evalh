from __future__ import annotations

from typing import ClassVar

from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
)
from eval_harness.evaluators.base import Evaluator


class ContainsTextEvaluator(Evaluator):
    type: ClassVar[str] = "contains_text"

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        raise NotImplementedError("ContainsTextEvaluator.evaluate lands in ev-4us")
