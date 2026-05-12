from __future__ import annotations

from datetime import UTC, datetime

from eval_harness.core.models import (
    EvalCase,
    ExpectedBehavior,
    Trace,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.evaluators.thinking_tokens_under import (
    ThinkingTokensUnderEvaluator,
)

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


def _trace(token_thinking: int | None) -> Trace:
    return Trace(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=10,
        input={"q": "hi"},
        output=TraceOutput(final_answer="x"),
        metrics=TraceMetrics(token_thinking=token_thinking),
    )


def _case() -> EvalCase:
    return EvalCase(id="c1", input={"q": "hi"}, expected=ExpectedBehavior())


async def test_pass_when_thinking_tokens_below_threshold() -> None:
    ev = ThinkingTokensUnderEvaluator(name="t", max_tokens=500)
    result = await ev.evaluate(_case(), _trace(token_thinking=120), None)
    assert result.passed is True
    assert "120" in result.reason
    assert result.detail == {"actual_tokens": 120, "threshold_tokens": 500}


async def test_fail_when_thinking_tokens_at_or_above_threshold() -> None:
    ev = ThinkingTokensUnderEvaluator(name="t", max_tokens=500)
    result = await ev.evaluate(_case(), _trace(token_thinking=500), None)
    assert result.passed is False
    assert ">= 500" in result.reason


async def test_fail_when_thinking_tokens_missing() -> None:
    ev = ThinkingTokensUnderEvaluator(name="t", max_tokens=500)
    result = await ev.evaluate(_case(), _trace(token_thinking=None), None)
    assert result.passed is False
    assert result.reason == "no thinking tokens reported"
