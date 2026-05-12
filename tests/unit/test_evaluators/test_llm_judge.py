from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, ExpectedBehavior, Trace, TraceOutput
from eval_harness.core.time import utc_now
from eval_harness.evaluators._judge_backends import (
    JudgeBackend,
    JudgeParseError,
    judge_backend_registry,
)
from eval_harness.evaluators.llm_judge import LlmJudgeEvaluator


class _FakeBackend:
    """Stub JudgeBackend: returns a canned response, records calls."""

    def __init__(self, model: str, *, response: dict[str, Any] | None = None,
                 raise_on_judge: Exception | None = None) -> None:
        self.model = model
        self.response = response or {}
        self.raise_on_judge = raise_on_judge
        self.calls: list[dict[str, Any]] = []

    async def judge(
        self,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "schema": schema, "max_tokens": max_tokens})
        if self.raise_on_judge is not None:
            raise self.raise_on_judge
        return self.response


@pytest.fixture
def fake_claude_backend() -> Iterator[dict[str, _FakeBackend | None]]:
    """Registers a stub factory under prefix 'claude'; restores prior state.

    Yields a one-element holder so tests can read the most recently constructed
    fake backend (and the test mutates `holder["response"]` etc. before evaluate
    runs).
    """
    holder: dict[str, Any] = {"backend": None, "response": {}, "raise_on_judge": None}
    prior = judge_backend_registry._factories.get("claude")

    def factory(model: str) -> JudgeBackend:
        b = _FakeBackend(
            model,
            response=holder.get("response"),
            raise_on_judge=holder.get("raise_on_judge"),
        )
        holder["backend"] = b
        return b

    judge_backend_registry.register("claude", factory)
    try:
        yield holder
    finally:
        if prior is None:
            judge_backend_registry.unregister("claude")
        else:
            judge_backend_registry.register("claude", prior)


def _case() -> EvalCase:
    return EvalCase(
        id="c1",
        input={"user_message": "tell me about ABC123"},
        expected=ExpectedBehavior(facts={"suburb": "Richmond"}),
    )


def _trace(answer: str = "Richmond is great") -> Trace:
    now = utc_now()
    return Trace(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=now,
        finished_at=now,
        latency_ms=0,
        input={"user_message": "hi"},
        output=TraceOutput(final_answer=answer),
    )


def test_validate_requires_model() -> None:
    with pytest.raises(ConfigError):
        LlmJudgeEvaluator.validate_config({"nl_assertions": ["a"]})


def test_validate_requires_assertions_or_rubric() -> None:
    with pytest.raises(ConfigError):
        LlmJudgeEvaluator.validate_config({"model": "claude-4-7"})


def test_validate_pass_when_k_of_n_in_range() -> None:
    LlmJudgeEvaluator.validate_config(
        {"model": "claude-4-7", "nl_assertions": ["a", "b", "c"], "pass_when": "k_of_n=2"}
    )
    with pytest.raises(ConfigError):
        LlmJudgeEvaluator.validate_config(
            {
                "model": "claude-4-7",
                "nl_assertions": ["a", "b"],
                "pass_when": "k_of_n=5",
            }
        )


def test_validate_rejects_unknown_model_prefix(fake_claude_backend: dict[str, Any]) -> None:
    LlmJudgeEvaluator.validate_config({"model": "gpt-5", "nl_assertions": ["a"]})
    with pytest.raises(ConfigError) as exc:
        LlmJudgeEvaluator(
            name="x", model="gpt-5", nl_assertions=["a"], pass_when="all"
        )
    assert "gpt" in str(exc.value).lower() or "no judge backend" in str(exc.value).lower()


async def test_pass_when_all_passes(fake_claude_backend: dict[str, Any]) -> None:
    fake_claude_backend["response"] = {
        "assertions": [
            {"text": "a", "passed": True, "reason": "yes"},
            {"text": "b", "passed": True, "reason": "yes"},
        ]
    }
    ev = LlmJudgeEvaluator(name="quality", model="claude-4-7", nl_assertions=["a", "b"])
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is True
    assert result.score == pytest.approx(1.0)
    assert result.error is None
    assert result.detail["judge_model"] == "claude-4-7"
    assert len(result.detail["assertions"]) == 2


async def test_pass_when_all_fails_on_required(fake_claude_backend: dict[str, Any]) -> None:
    fake_claude_backend["response"] = {
        "assertions": [
            {"text": "a", "passed": True, "reason": "yes"},
            {"text": "b", "passed": False, "reason": "no"},
        ]
    }
    ev = LlmJudgeEvaluator(name="quality", model="claude-4-7", nl_assertions=["a", "b"])
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is False
    assert result.score == pytest.approx(0.5)
    assert "1/2 assertions failed" in result.reason


async def test_pass_when_majority(fake_claude_backend: dict[str, Any]) -> None:
    fake_claude_backend["response"] = {
        "assertions": [
            {"text": "a", "passed": True, "reason": ""},
            {"text": "b", "passed": True, "reason": ""},
            {"text": "c", "passed": False, "reason": ""},
        ]
    }
    ev = LlmJudgeEvaluator(
        name="q",
        model="claude-4-7",
        nl_assertions=["a", "b", "c"],
        pass_when="majority",
    )
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is True


async def test_pass_when_any(fake_claude_backend: dict[str, Any]) -> None:
    fake_claude_backend["response"] = {
        "assertions": [
            {"text": "a", "passed": False, "reason": ""},
            {"text": "b", "passed": True, "reason": ""},
        ]
    }
    ev = LlmJudgeEvaluator(
        name="q", model="claude-4-7", nl_assertions=["a", "b"], pass_when="any"
    )
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is True


async def test_pass_when_k_of_n(fake_claude_backend: dict[str, Any]) -> None:
    fake_claude_backend["response"] = {
        "assertions": [
            {"text": "a", "passed": True, "reason": ""},
            {"text": "b", "passed": True, "reason": ""},
            {"text": "c", "passed": False, "reason": ""},
        ]
    }
    ev_pass = LlmJudgeEvaluator(
        name="q",
        model="claude-4-7",
        nl_assertions=["a", "b", "c"],
        pass_when="k_of_n=2",
    )
    assert (await ev_pass.evaluate(_case(), _trace(), None)).passed is True

    fake_claude_backend["response"] = {
        "assertions": [
            {"text": "a", "passed": True, "reason": ""},
            {"text": "b", "passed": False, "reason": ""},
            {"text": "c", "passed": False, "reason": ""},
        ]
    }
    ev_fail = LlmJudgeEvaluator(
        name="q",
        model="claude-4-7",
        nl_assertions=["a", "b", "c"],
        pass_when="k_of_n=2",
    )
    assert (await ev_fail.evaluate(_case(), _trace(), None)).passed is False


async def test_optional_assertion_does_not_fail_required_pass(
    fake_claude_backend: dict[str, Any],
) -> None:
    fake_claude_backend["response"] = {
        "assertions": [
            {"text": "must", "passed": True, "reason": ""},
            {"text": "nice", "passed": False, "reason": ""},
        ]
    }
    ev = LlmJudgeEvaluator(
        name="q",
        model="claude-4-7",
        nl_assertions=[
            {"text": "must", "required": True},
            {"text": "nice", "required": False},
        ],
        pass_when="all",
    )
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is True
    assert result.score == pytest.approx(0.5)
    assert "optional misses" in result.reason


async def test_malformed_judge_json_yields_error_result(
    fake_claude_backend: dict[str, Any],
) -> None:
    fake_claude_backend["response"] = {"not_assertions": []}
    ev = LlmJudgeEvaluator(name="q", model="claude-4-7", nl_assertions=["a", "b"])
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is False
    assert result.error is not None
    assert result.error.type == "adapter_error"


async def test_judge_raises_yields_error_result(
    fake_claude_backend: dict[str, Any],
) -> None:
    fake_claude_backend["raise_on_judge"] = JudgeParseError("boom")
    ev = LlmJudgeEvaluator(name="q", model="claude-4-7", nl_assertions=["a"])
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is False
    assert result.error is not None
    assert "JudgeParseError" in (result.error.message or "")


async def test_cost_limit_blocks_call(fake_claude_backend: dict[str, Any]) -> None:
    fake_claude_backend["response"] = {
        "assertions": [{"text": "a", "passed": True, "reason": ""}]
    }
    ev = LlmJudgeEvaluator(
        name="q",
        model="claude-4-7",
        nl_assertions=["a"],
        cost_limit_usd=0.0000001,
    )
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is False
    assert result.error is not None
    assert result.error.type == "cost_limit_exceeded"
    # Backend was never called.
    assert fake_claude_backend["backend"] is None or fake_claude_backend["backend"].calls == []


async def test_rubric_mode(fake_claude_backend: dict[str, Any]) -> None:
    fake_claude_backend["response"] = {"score": 4.0, "rubric_reason": "good voice"}
    ev = LlmJudgeEvaluator(
        name="voice",
        model="claude-4-7",
        rubric="Score 1-5 on voice.",
        scale={"min": 1, "max": 5},
        pass_threshold=3,
    )
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is True
    assert result.detail["rubric"]["score"] == 4.0


async def test_rubric_below_threshold(fake_claude_backend: dict[str, Any]) -> None:
    fake_claude_backend["response"] = {"score": 2.0, "rubric_reason": "weak"}
    ev = LlmJudgeEvaluator(
        name="voice",
        model="claude-4-7",
        rubric="Score 1-5 on voice.",
        scale={"min": 1, "max": 5},
        pass_threshold=3,
    )
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is False


async def test_both_modes(fake_claude_backend: dict[str, Any]) -> None:
    fake_claude_backend["response"] = {
        "assertions": [{"text": "a", "passed": True, "reason": ""}],
        "score": 4.0,
        "rubric_reason": "ok",
    }
    ev = LlmJudgeEvaluator(
        name="q",
        model="claude-4-7",
        nl_assertions=["a"],
        rubric="Score 1-5.",
        scale={"min": 1, "max": 5},
        rubric_pass_threshold=3,
    )
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is True
    assert "assertions" in result.detail
    assert result.detail["rubric"]["score"] == 4.0


async def test_both_modes_rubric_fails_overall_fails(
    fake_claude_backend: dict[str, Any],
) -> None:
    fake_claude_backend["response"] = {
        "assertions": [{"text": "a", "passed": True, "reason": ""}],
        "score": 1.0,
        "rubric_reason": "bad",
    }
    ev = LlmJudgeEvaluator(
        name="q",
        model="claude-4-7",
        nl_assertions=["a"],
        rubric="Score 1-5.",
        scale={"min": 1, "max": 5},
        rubric_pass_threshold=3,
    )
    result = await ev.evaluate(_case(), _trace(), None)
    assert result.passed is False


def test_evaluator_factory_registers_llm_judge() -> None:
    from eval_harness.factories import evaluator_factory

    assert "llm_judge" in evaluator_factory.registry.names()


def test_judge_backend_registry_loads_anthropic_entry_point() -> None:
    # Importing the evaluators package triggers load_entry_points().
    import eval_harness.evaluators  # noqa: F401

    assert "claude" in judge_backend_registry.names()


def test_anthropic_backend_raises_configerror_if_sdk_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins
    import importlib

    real_import = builtins.__import__

    def fake_import(
        name: str, globals: Any = None, locals: Any = None,
        fromlist: tuple[str, ...] = (), level: int = 0,
    ) -> Any:
        if name == "anthropic":
            raise ImportError("simulated missing anthropic")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    mod = importlib.reload(
        importlib.import_module(
            "eval_harness.evaluators._judge_backends.anthropic_backend"
        )
    )
    with pytest.raises(ConfigError) as exc:
        mod.AnthropicJudgeBackend("claude-4-7")
    assert "anthropic" in str(exc.value).lower()
    assert "eval-harness[anthropic]" in str(exc.value)
