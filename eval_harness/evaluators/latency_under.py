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


class LatencyUnderEvaluator(Evaluator):
    type: ClassVar[str] = "latency_under"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        max_ms = config.get("max_ms")
        if not isinstance(max_ms, int) or isinstance(max_ms, bool) or max_ms <= 0:
            raise ConfigError("latency_under: 'max_ms' (positive int) is required")
        if "field" in config and not isinstance(config["field"], str):
            raise ConfigError("latency_under: 'field' must be a string")

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()
        max_ms: int = self._config["max_ms"]
        actual_ms = trace.latency_ms

        passed = actual_ms < max_ms
        reason = (
            f"latency {actual_ms}ms < {max_ms}ms"
            if passed
            else f"latency {actual_ms}ms >= {max_ms}ms"
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
            detail={"actual_ms": actual_ms, "threshold_ms": max_ms},
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )
