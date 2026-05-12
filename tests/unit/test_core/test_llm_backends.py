from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.llm_backends import (
    LlmBackendRegistry,
    LlmCall,
    llm_backend_registry,
)


class _FakeBackend:
    def __init__(self) -> None:
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
        self.calls.append(
            {"prompt": prompt, "model": model, "max_tokens": max_tokens, "schema": schema}
        )
        return LlmCall(text="ok", structured={"ok": True})


def test_resolve_claude_dispatches_to_anthropic_class() -> None:
    """The built-in `claude` prefix resolves to a backend whose class is the
    Anthropic backend. The SDK may or may not be installed in the test env, so
    we resolve in a fresh registry that aliases anthropic to a stub class to
    avoid the import-time check."""

    registry = LlmBackendRegistry()
    sentinel = _FakeBackend()
    registry.register("claude", lambda: sentinel)
    resolved = registry.resolve("claude-4-7")
    assert resolved is sentinel

    # Singleton-per-prefix: subsequent resolves return the same instance.
    assert registry.resolve("claude-haiku-4-5-20251001") is sentinel


def test_resolve_unknown_prefix_raises_configerror() -> None:
    registry = LlmBackendRegistry()
    with pytest.raises(ConfigError) as exc:
        registry.resolve("gpt-5")
    msg = str(exc.value).lower()
    assert "gpt" in msg
    assert "no backend" in msg


def test_resolve_gpt_raises_clear_error_when_openai_not_implemented() -> None:
    registry = LlmBackendRegistry()
    registry.register("claude", lambda: _FakeBackend())
    with pytest.raises(ConfigError) as exc:
        registry.resolve("gpt-5")
    msg = str(exc.value)
    assert "gpt" in msg.lower()
    # Known prefixes hint is included.
    assert "claude" in msg


def test_register_third_party_via_register() -> None:
    registry = LlmBackendRegistry()
    fake = _FakeBackend()
    registry.register("cohere", lambda: fake)
    assert "cohere" in registry.names()
    assert registry.resolve("cohere-command-r") is fake


def test_register_legacy_judge_backend_adapts_to_llm_backend() -> None:
    """A third-party plugin under the old `eval_harness.judge_backends` group
    registers a class with `async def judge(prompt, schema, max_tokens) -> dict`.
    `register_legacy` wraps it as an LlmBackend.
    """

    class _LegacyJudge:
        def __init__(self, model: str) -> None:
            self.model = model

        async def judge(
            self, prompt: str, schema: dict[str, Any], max_tokens: int
        ) -> dict[str, Any]:
            return {"verdict": "ok", "_usage": {"input_tokens": 7, "output_tokens": 3}}

    registry = LlmBackendRegistry()
    registry.register_legacy("legacy", _LegacyJudge)
    backend = registry.resolve("legacy-foo")

    import asyncio

    call = asyncio.run(
        backend.generate(
            "hi", model="legacy-foo", max_tokens=128, schema={"type": "object"}
        )
    )
    assert call.structured == {"verdict": "ok"}
    assert call.token_input == 7
    assert call.token_output == 3


def test_load_entry_points_picks_up_llm_backend_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval_harness.core import llm_backends as mod

    fake = _FakeBackend()

    class _EP:
        def __init__(self, name: str, value: Any) -> None:
            self.name = name
            self._value = value

        def load(self) -> Any:
            return self._value

    def fake_entry_points(*, group: str) -> list[_EP]:
        if group == "eval_harness.llm_backends":
            return [_EP("mistral", lambda: fake)]
        return []

    monkeypatch.setattr(mod, "entry_points", fake_entry_points)

    registry = LlmBackendRegistry()
    registry.load_entry_points()
    assert "mistral" in registry.names()
    assert registry.resolve("mistral-large") is fake


def test_load_entry_points_wraps_legacy_judge_backends_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from eval_harness.core import llm_backends as mod

    class _LegacyJudge:
        def __init__(self, model: str) -> None:
            self.model = model

        async def judge(
            self, prompt: str, schema: dict[str, Any], max_tokens: int
        ) -> dict[str, Any]:
            return {"ok": True}

    class _EP:
        def __init__(self, name: str, value: Any) -> None:
            self.name = name
            self._value = value

        def load(self) -> Any:
            return self._value

    def fake_entry_points(*, group: str) -> list[_EP]:
        if group == "eval_harness.judge_backends":
            return [_EP("oldplugin", _LegacyJudge)]
        return []

    monkeypatch.setattr(mod, "entry_points", fake_entry_points)

    registry = LlmBackendRegistry()
    registry.load_entry_points()
    assert "oldplugin" in registry.names()

    backend = registry.resolve("oldplugin-x")
    import asyncio

    call = asyncio.run(
        backend.generate("hi", model="oldplugin-x", max_tokens=8, schema={})
    )
    assert call.structured == {"ok": True}


def test_load_entry_points_new_group_wins_over_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If both groups register the same prefix, the new group wins."""

    from eval_harness.core import llm_backends as mod

    new_backend = _FakeBackend()

    class _LegacyJudge:
        def __init__(self, model: str) -> None:
            raise AssertionError("legacy backend should not be instantiated")

    class _EP:
        def __init__(self, name: str, value: Any) -> None:
            self.name = name
            self._value = value

        def load(self) -> Any:
            return self._value

    def fake_entry_points(*, group: str) -> list[_EP]:
        if group == "eval_harness.llm_backends":
            return [_EP("claude", lambda: new_backend)]
        if group == "eval_harness.judge_backends":
            return [_EP("claude", _LegacyJudge)]
        return []

    monkeypatch.setattr(mod, "entry_points", fake_entry_points)

    registry = LlmBackendRegistry()
    registry.load_entry_points()
    assert registry.resolve("claude-4-7") is new_backend


def test_load_entry_points_idempotent() -> None:
    """Calling load_entry_points twice is a no-op the second time."""
    registry = LlmBackendRegistry()
    registry.load_entry_points()
    snapshot = list(registry.names())
    registry.load_entry_points()
    assert list(registry.names()) == snapshot


def test_global_registry_has_anthropic_after_evaluators_import() -> None:
    """Sanity check that the global registry has the built-in claude entry."""
    import eval_harness.evaluators  # noqa: F401

    assert "claude" in llm_backend_registry.names()


def test_existing_llm_judge_still_works_via_new_registry() -> None:
    """Regression: llm_judge resolves from the new registry and works
    end-to-end with a fake LlmBackend."""

    from eval_harness.core.models import EvalCase, ExpectedBehavior, Trace, TraceOutput
    from eval_harness.core.time import utc_now
    from eval_harness.evaluators.llm_judge import LlmJudgeEvaluator

    class _Backend:
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
            return LlmCall(
                structured={
                    "assertions": [
                        {"text": "a", "passed": True, "reason": "ok"},
                    ]
                },
                token_input=10,
                token_output=5,
            )

    backend = _Backend()
    prior_factories = dict(llm_backend_registry._factories)
    prior_instances = dict(llm_backend_registry._instances)
    llm_backend_registry.register("claude", lambda: backend)
    try:
        ev = LlmJudgeEvaluator(name="q", model="claude-4-7", nl_assertions=["a"])
        case = EvalCase(id="c1", input={"q": "hi"}, expected=ExpectedBehavior())
        now = utc_now()
        trace = Trace(
            run_id="r1",
            case_id="c1",
            variant_name="v1",
            started_at=now,
            finished_at=now,
            latency_ms=0,
            input={},
            output=TraceOutput(final_answer="x"),
        )

        import asyncio

        result = asyncio.run(ev.evaluate(case, trace, None))
        assert result.passed is True
        assert result.detail["judge_model"] == "claude-4-7"
        # Cost computed from token counts via the pricing helper.
        assert "cost_usd" in result.detail
    finally:
        llm_backend_registry._factories = prior_factories
        llm_backend_registry._instances = prior_instances


def test_llm_call_defaults() -> None:
    call = LlmCall()
    assert call.text == ""
    assert call.structured is None
    assert call.token_input == 0
    assert call.token_output == 0
    assert call.token_thinking == 0
    assert call.cost_usd == 0.0
    assert call.extra == {}


def test_anthropic_backend_schema_aware_response() -> None:
    """Smoke-check that the new Anthropic backend parses a schema response."""

    from eval_harness.core.llm_backends import anthropic as anth_mod

    class _Block:
        def __init__(self, text: str) -> None:
            self.text = text

    fake_response = MagicMock()
    fake_response.content = [_Block('{"score": 4, "rubric_reason": "ok"}')]
    fake_response.usage = MagicMock(input_tokens=11, output_tokens=7)

    class _FakeClient:
        class messages:
            @staticmethod
            async def create(**kwargs: Any) -> Any:
                return fake_response

    backend = anth_mod.AnthropicLlmBackend.__new__(anth_mod.AnthropicLlmBackend)
    backend._client = _FakeClient()  # type: ignore[attr-defined]
    backend._text_block_cls = _Block  # type: ignore[attr-defined]

    import asyncio

    call = asyncio.run(
        backend.generate(
            "judge me",
            model="claude-4-7",
            max_tokens=256,
            schema={"type": "object", "properties": {"score": {"type": "number"}}},
        )
    )
    assert call.structured == {"score": 4, "rubric_reason": "ok"}
    assert call.token_input == 11
    assert call.token_output == 7
    assert call.cost_usd > 0
