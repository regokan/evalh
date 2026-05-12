from __future__ import annotations

from typing import ClassVar

from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.base import Evaluator


class ThinkingPresentEvaluator(Evaluator):
    type: ClassVar[str] = "thinking_present"

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()
        thinking = trace.output.thinking
        passed = isinstance(thinking, str) and bool(thinking)
        if passed:
            reason = f"thinking present ({len(thinking or '')} chars)"
        elif thinking is None:
            reason = "output.thinking is missing"
        else:
            reason = "output.thinking is empty"

        finished = utc_now()
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=passed,
            reason=reason,
            detail={"length": len(thinking) if isinstance(thinking, str) else 0},
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )
