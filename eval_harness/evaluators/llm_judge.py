from __future__ import annotations

from typing import ClassVar

from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
)
from eval_harness.evaluators.base import Evaluator


class LlmJudgeEvaluator(Evaluator):
    type: ClassVar[str] = "llm_judge"

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        raise NotImplementedError("LlmJudgeEvaluator.evaluate lands in ev-ct9")
