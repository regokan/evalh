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


class CostUnderEvaluator(Evaluator):
    type: ClassVar[str] = "cost_under"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        max_usd = config.get("max_usd")
        if (
            not isinstance(max_usd, int | float)
            or isinstance(max_usd, bool)
            or max_usd <= 0
        ):
            raise ConfigError("cost_under: 'max_usd' (positive number) is required")

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()
        max_usd: float = float(self._config["max_usd"])
        actual_usd = trace.metrics.cost_usd

        if actual_usd is None:
            passed = False
            reason = "cost not reported by adapter"
        else:
            passed = actual_usd < max_usd
            reason = (
                f"cost ${actual_usd:.4f} < ${max_usd:.4f}"
                if passed
                else f"cost ${actual_usd:.4f} >= ${max_usd:.4f}"
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
            detail={"actual_usd": actual_usd, "threshold_usd": max_usd},
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )
