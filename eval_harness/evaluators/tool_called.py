from __future__ import annotations

from typing import ClassVar

from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
)
from eval_harness.evaluators.base import Evaluator


class ToolCalledEvaluator(Evaluator):
    type: ClassVar[str] = "tool_called"

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        raise NotImplementedError("ToolCalledEvaluator.evaluate lands in ev-4us")
