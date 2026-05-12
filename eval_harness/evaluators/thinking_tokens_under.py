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


class ThinkingTokensUnderEvaluator(Evaluator):
    type: ClassVar[str] = "thinking_tokens_under"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        max_tokens = config.get("max_tokens")
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or max_tokens <= 0
        ):
            raise ConfigError(
                "thinking_tokens_under: 'max_tokens' (positive int) is required"
            )

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()
        max_tokens: int = self._config["max_tokens"]
        actual = trace.metrics.token_thinking

        if actual is None:
            passed = False
            reason = "no thinking tokens reported"
        else:
            passed = actual < max_tokens
            reason = (
                f"thinking tokens {actual} < {max_tokens}"
                if passed
                else f"thinking tokens {actual} >= {max_tokens}"
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
            detail={"actual_tokens": actual, "threshold_tokens": max_tokens},
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )
