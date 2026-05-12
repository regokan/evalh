from __future__ import annotations

from datetime import UTC, datetime

from eval_harness.core.models import EvalCase, ExpectedBehavior, Trace, TraceOutput
from eval_harness.evaluators.thinking_present import ThinkingPresentEvaluator

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


def _trace(thinking: str | None) -> Trace:
    return Trace(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=10,
        input={"q": "hi"},
        output=TraceOutput(final_answer="x", thinking=thinking),
    )


def _case() -> EvalCase:
    return EvalCase(id="c1", input={"q": "hi"}, expected=ExpectedBehavior())


async def test_pass_when_thinking_present() -> None:
    ev = ThinkingPresentEvaluator(name="t")
    result = await ev.evaluate(_case(), _trace("Let me consider this..."), None)
    assert result.passed is True
    assert result.detail == {"length": len("Let me consider this...")}


async def test_fail_when_thinking_empty() -> None:
    ev = ThinkingPresentEvaluator(name="t")
    result = await ev.evaluate(_case(), _trace(""), None)
    assert result.passed is False
    assert result.reason == "output.thinking is empty"


async def test_fail_when_thinking_missing() -> None:
    ev = ThinkingPresentEvaluator(name="t")
    result = await ev.evaluate(_case(), _trace(None), None)
    assert result.passed is False
    assert result.reason == "output.thinking is missing"
