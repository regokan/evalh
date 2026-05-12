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


class TokensPerSecondAboveEvaluator(Evaluator):
    """Pass when ``trace.metrics.tokens_per_second >= min_tps``.

    Streaming-only: non-streaming systems leave the field as ``None`` and
    the evaluator fails with reason ``'not streaming'``.
    """

    type: ClassVar[str] = "tokens_per_second_above"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        min_tps = config.get("min_tps")
        if (
            not isinstance(min_tps, int | float)
            or isinstance(min_tps, bool)
            or min_tps <= 0
        ):
            raise ConfigError(
                "tokens_per_second_above: 'min_tps' (positive number) is required"
            )

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()
        min_tps: float = float(self._config["min_tps"])
        actual = trace.metrics.tokens_per_second

        if actual is None:
            passed = False
            reason = "not streaming"
        else:
            passed = actual >= min_tps
            reason = (
                f"tokens_per_second {actual:.2f} >= {min_tps:.2f}"
                if passed
                else f"tokens_per_second {actual:.2f} < {min_tps:.2f}"
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
            detail={"actual_tps": actual, "threshold_tps": min_tps},
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )
