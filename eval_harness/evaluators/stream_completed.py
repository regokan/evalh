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


class StreamCompletedEvaluator(Evaluator):
    """Pass when ``trace.metrics.stream_completed is True``.

    Catches truncated streams: a non-streaming system has ``None`` and
    fails with reason ``'not streaming'``; a streaming run whose stream
    didn't end cleanly has ``False`` and fails with ``'stream truncated'``.
    """

    type: ClassVar[str] = "stream_completed"

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()
        actual = trace.metrics.stream_completed

        if actual is None:
            passed = False
            reason = "not streaming"
        elif actual is True:
            passed = True
            reason = "stream completed"
        else:
            passed = False
            reason = "stream truncated"

        finished = utc_now()
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=passed,
            reason=reason,
            detail={"stream_completed": actual},
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )
