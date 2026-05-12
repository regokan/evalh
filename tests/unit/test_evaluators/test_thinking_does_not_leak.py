from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest

from eval_harness.core.llm_backends import LlmCall, llm_backend_registry
from eval_harness.core.models import EvalCase, ExpectedBehavior, Trace, TraceOutput
from eval_harness.evaluators.thinking_does_not_leak import (
    ThinkingDoesNotLeakEvaluator,
)

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


class _FakeBackend:
    def __init__(self, holder: dict[str, Any]) -> None:
        self._holder = holder
        self.calls: list[dict[str, Any]] = []

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        schema: dict[str, Any] | None = None,
        cost_limit_usd: float | None = None,
    ) -> LlmCall:
        self.calls.append({"prompt": prompt, "model": model, "schema": schema})
        if (exc := self._holder.get("raise_on_call")) is not None:
            raise exc
        return LlmCall(structured=self._holder.get("response"))


@pytest.fixture
def fake_claude_backend() -> Iterator[dict[str, Any]]:
    holder: dict[str, Any] = {"response": {"leaks": False, "reason": ""}, "raise_on_call": None}
    prior_factories = dict(llm_backend_registry._factories)
    prior_instances = dict(llm_backend_registry._instances)
    backend = _FakeBackend(holder)
    holder["backend"] = backend
    llm_backend_registry.register("claude", lambda: backend)
    try:
        yield holder
    finally:
        llm_backend_registry._factories = prior_factories
        llm_backend_registry._instances = prior_instances


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


async def test_pass_when_no_pattern_match_and_judge_clean(
    fake_claude_backend: dict[str, Any],
) -> None:
    fake_claude_backend["response"] = {"leaks": False, "reason": "no sensitive content"}
    ev = ThinkingDoesNotLeakEvaluator(
        name="leak",
        model="claude-4-7",
        forbidden_patterns=["SECRET_TOKEN", "re:sk-[a-z0-9]{4,}"],
    )
    result = await ev.evaluate(
        _case(), _trace("just reasoning about the suburb prices"), None
    )
    assert result.passed is True
    assert result.reason == "no leaks"
    assert result.detail["pattern_matches"] == []
    assert result.detail["judge_verdict"] is True
    # The mocked backend was reached — confirms we used the LlmBackend seam.
    backend = fake_claude_backend["backend"]
    assert len(backend.calls) == 1


async def test_fail_on_literal_pattern_match(
    fake_claude_backend: dict[str, Any],
) -> None:
    fake_claude_backend["response"] = {"leaks": False, "reason": "looks fine"}
    ev = ThinkingDoesNotLeakEvaluator(
        name="leak", forbidden_patterns=["SECRET_TOKEN", "re:sk-[a-z0-9]{4,}"]
    )
    result = await ev.evaluate(
        _case(),
        _trace("internal note: SECRET_TOKEN=abc and also sk-deadbeef"),
        None,
    )
    assert result.passed is False
    matches = result.detail["pattern_matches"]
    patterns = {m["pattern"] for m in matches}
    assert "SECRET_TOKEN" in patterns
    assert "re:sk-[a-z0-9]{4,}" in patterns
    for m in matches:
        assert isinstance(m["span"], list) and len(m["span"]) == 2


async def test_error_when_judge_response_malformed(
    fake_claude_backend: dict[str, Any],
) -> None:
    fake_claude_backend["response"] = {"unexpected_field": True}
    ev = ThinkingDoesNotLeakEvaluator(
        name="leak", forbidden_patterns=["NEVER_OCCURS"]
    )
    result = await ev.evaluate(_case(), _trace("benign thinking"), None)
    assert result.passed is False
    assert result.error is not None
    assert "missing 'leaks'" in result.error.message
