from __future__ import annotations

from typing import Any, ClassVar

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.base import Evaluator


class LatencyFirstTokenUnderEvaluator(Evaluator):
    """Pass when ``trace.metrics.latency_first_token_ms < max_ms``.

    Streaming-only: non-streaming systems leave the field as ``None`` and
    the evaluator fails with reason ``'not a streaming system'``. Same
    mechanical shape as ``latency_under`` from v0.1.
    """

    type: ClassVar[str] = "latency_first_token_under"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        max_ms = config.get("max_ms")
        if not isinstance(max_ms, int) or isinstance(max_ms, bool) or max_ms <= 0:
            raise ConfigError(
                "latency_first_token_under: 'max_ms' (positive int) is required"
            )

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()
        max_ms: int = self._config["max_ms"]
        actual = trace.metrics.latency_first_token_ms

        if actual is None:
            passed = False
            reason = "not a streaming system"
        else:
            passed = actual < max_ms
            reason = (
                f"latency_first_token {actual}ms < {max_ms}ms"
                if passed
                else f"latency_first_token {actual}ms >= {max_ms}ms"
            )

        finished = utc_now()
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=passed,
            reason=reason,
            detail={"actual_ms": actual, "threshold_ms": max_ms},
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )
