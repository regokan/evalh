"""UserSimulatorSystemAdapter tests.

Mocks at the seam (per .claude/rules/testing.md > Mocking conventions): a fake
LlmBackend registered via `llm_backend_registry` plays both the user and the
judge; a stub SystemAdapter registered via `system_adapter_factory` plays
the inner system. No real LLMs, no network.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Any, ClassVar, Self

import pytest

from eval_harness.adapters.system.user_simulator_adapter import (
    UserSimulatorSystemAdapter,
)
from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.llm_backends import LlmCall, llm_backend_registry
from eval_harness.core.models import (
    EvalCase,
    RunVariant,
    ToolCall,
    Trace,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.core.time import utc_now
from eval_harness.factories import system_adapter_factory

# ---------- fakes ----------


class _ScriptedBackend:
    """LlmBackend stub: emits canned `.text` per call in script order. Records
    every call's prompt + system for assertions."""

    def __init__(self, script: list[str]) -> None:
        self._script = list(script)
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
        self.calls.append({"prompt": prompt, "system": system, "model": model})
        if not self._script:
            raise AssertionError(
                "scripted backend ran out of canned responses "
                f"(call #{len(self.calls)} prompt: {prompt[:100]!r})"
            )
        text = self._script.pop(0)
        return LlmCall(text=text, token_input=1, token_output=2)


@contextmanager
def _register_backend(prefix: str, backend: _ScriptedBackend) -> Iterator[None]:
    """Temporarily registers a singleton-returning factory under `prefix`."""
    registry = llm_backend_registry
    prior_factory = registry._factories.get(prefix)
    prior_instance = registry._instances.get(prefix)
    registry.register(prefix, lambda: backend)
    try:
        yield
    finally:
        if prior_factory is None:
            registry._factories.pop(prefix, None)
        else:
            registry._factories[prefix] = prior_factory
        if prior_instance is None:
            registry._instances.pop(prefix, None)
        else:
            registry._instances[prefix] = prior_instance


class _StubInner:
    """Inner SystemAdapter stub: emits canned assistant replies per turn and
    records every (case, variant) pair it sees."""

    next_replies: ClassVar[list[str]] = []
    seen_cases: ClassVar[list[dict[str, Any]]] = []
    seen_inits: ClassVar[list[dict[str, Any]]] = []
    aenter_count: ClassVar[int] = 0
    aexit_count: ClassVar[int] = 0

    def __init__(self, name: str, **config: Any) -> None:
        self.name = name
        self._config = config
        _StubInner.seen_inits.append({"name": name, **config})

    async def __aenter__(self) -> Self:
        _StubInner.aenter_count += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        _StubInner.aexit_count += 1

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        _StubInner.seen_cases.append({
            "case_id": case.id,
            "user_message": case.input.get("user_message"),
            "conversation_len": len(case.input.get("conversation") or []),
        })
        if not _StubInner.next_replies:
            raise AssertionError("stub inner ran out of canned replies")
        reply = _StubInner.next_replies.pop(0)
        now = utc_now()
        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=now,
            finished_at=now,
            latency_ms=0,
            input=case.input,
            output=TraceOutput(final_answer=reply),
            metrics=TraceMetrics(token_input=10, token_output=20, cost_usd=0.01),
            tool_calls=[ToolCall(name="dummy", arguments={"q": case.id})],
        )


@contextmanager
def _register_inner() -> Iterator[None]:
    registry = system_adapter_factory.registry
    prior = registry._items.get("stub_inner")
    registry.register("stub_inner", _StubInner)
    try:
        yield
    finally:
        if prior is None:
            registry._items.pop("stub_inner", None)
        else:
            registry._items["stub_inner"] = prior


@pytest.fixture(autouse=True)
def _reset_stub() -> Iterator[None]:
    _StubInner.next_replies = []
    _StubInner.seen_cases = []
    _StubInner.seen_inits = []
    _StubInner.aenter_count = 0
    _StubInner.aexit_count = 0
    yield


# ---------- helpers ----------


def _case() -> EvalCase:
    return EvalCase(id="c1", input={"user_message": "Hello, what can you do?"})


def _variant() -> RunVariant:
    return RunVariant(name="v1", adapter="user_simulator", config={})


def _adapter(**overrides: Any) -> UserSimulatorSystemAdapter:
    cfg = {
        "user_model": "claude-4-7",
        "user_persona_prompt": "You are a curious user.",
        "max_turns": 5,
        "stopping_criterion": {"type": "turn_count", "n": 3},
        "inner_system": {"adapter": "stub_inner"},
    }
    cfg.update(overrides)
    return UserSimulatorSystemAdapter(**cfg)


# ---------- config / lifecycle ----------


def test_missing_user_model_raises() -> None:
    with pytest.raises(ConfigError, match="user_model"):
        UserSimulatorSystemAdapter(
            user_persona_prompt="x",
            stopping_criterion={"type": "turn_count", "n": 1},
            inner_system={"adapter": "stub_inner"},
        )


def test_missing_persona_raises() -> None:
    with pytest.raises(ConfigError, match="user_persona_prompt"):
        UserSimulatorSystemAdapter(
            user_model="claude-4-7",
            stopping_criterion={"type": "turn_count", "n": 1},
            inner_system={"adapter": "stub_inner"},
        )


def test_missing_inner_system_raises() -> None:
    with (
        _register_backend("claude", _ScriptedBackend([])),
        pytest.raises(ConfigError, match="inner_system"),
    ):
        UserSimulatorSystemAdapter(
            user_model="claude-4-7",
            user_persona_prompt="x",
            stopping_criterion={"type": "turn_count", "n": 1},
        )


def test_invalid_stop_type_raises() -> None:
    with (
        _register_backend("claude", _ScriptedBackend([])),
        _register_inner(),
        pytest.raises(ConfigError, match=r"stopping_criterion\.type"),
    ):
        UserSimulatorSystemAdapter(
            user_model="claude-4-7",
            user_persona_prompt="x",
            stopping_criterion={"type": "bogus"},
            inner_system={"adapter": "stub_inner"},
        )


async def test_lifecycle_enters_and_exits_inner_adapter() -> None:
    with _register_backend("claude", _ScriptedBackend([])), _register_inner():
        async with _adapter():
            assert _StubInner.aenter_count == 1
            assert _StubInner.aexit_count == 0
        assert _StubInner.aexit_count == 1


async def test_run_outside_context_raises() -> None:
    with _register_backend("claude", _ScriptedBackend([])), _register_inner():
        adapter = _adapter()
        with pytest.raises(AdapterError, match="outside of `async with`"):
            await adapter.run(_case(), _variant(), None)


# ---------- conversation behaviour ----------


async def test_runs_to_max_turns_when_no_stop_fires() -> None:
    """turn_count(n=3) stops after 3 assistant replies."""
    _StubInner.next_replies = ["r1", "r2", "r3", "r4", "r5"]
    user = _ScriptedBackend(["u2", "u3", "u4", "u5"])
    with _register_backend("claude", user), _register_inner():
        async with _adapter(stopping_criterion={"type": "turn_count", "n": 3}) as a:
            trace = await a.run(_case(), _variant(), None)

    assert len(_StubInner.seen_cases) == 3
    assert trace.extra["user_simulator"]["turns"] == 3
    assert trace.extra["user_simulator"]["stop_reason"] == "turn_count(n=3)"
    assert trace.output.final_answer == "r3"


async def test_max_turns_cap_overrides_stopping_criterion() -> None:
    """Hard ceiling: if stop never fires, max_turns ends the loop."""
    _StubInner.next_replies = ["r1", "r2"]
    user = _ScriptedBackend(["u2"])
    with _register_backend("claude", user), _register_inner():
        async with _adapter(
            max_turns=2,
            stopping_criterion={"type": "content_match", "patterns": ["NEVER"]},
        ) as a:
            trace = await a.run(_case(), _variant(), None)
    assert trace.extra["user_simulator"]["turns"] == 2
    assert trace.extra["user_simulator"]["stop_reason"] == "max_turns_reached"


async def test_stops_on_content_match_case_insensitive() -> None:
    _StubInner.next_replies = ["hello", "All Done — thanks!"]
    user = _ScriptedBackend(["follow up question"])  # one user follow-up
    with _register_backend("claude", user), _register_inner():
        async with _adapter(
            stopping_criterion={"type": "content_match", "patterns": ["all done"]}
        ) as a:
            trace = await a.run(_case(), _variant(), None)

    assert _StubInner.seen_cases[-1]["user_message"] == "follow up question"
    assert trace.extra["user_simulator"]["turns"] == 2
    assert trace.extra["user_simulator"]["stop_reason"] == "content_match('all done')"
    assert trace.output.final_answer == "All Done — thanks!"


async def test_stops_on_judge_call_yes_verdict() -> None:
    _StubInner.next_replies = ["thanks!", "extra reply"]
    user = _ScriptedBackend(["u2"])
    judge = _ScriptedBackend(["yes"])
    with (
        _register_backend("claude", user),
        _register_backend("gpt", judge),
        _register_inner(),
    ):
        async with _adapter(
            stopping_criterion={
                "type": "judge",
                "model": "gpt-judge",
                "question": "Is the user satisfied?",
            }
        ) as a:
            trace = await a.run(_case(), _variant(), None)

    assert trace.extra["user_simulator"]["turns"] == 1
    assert trace.extra["user_simulator"]["stop_reason"] == "judge(gpt-judge)"
    assert len(judge.calls) == 1
    assert "Is the user satisfied?" in judge.calls[0]["prompt"]


async def test_judge_no_verdict_continues_loop() -> None:
    _StubInner.next_replies = ["r1", "r2"]
    user = _ScriptedBackend(["u2"])
    judge = _ScriptedBackend(["no", "no"])
    with (
        _register_backend("claude", user),
        _register_backend("gpt", judge),
        _register_inner(),
    ):
        async with _adapter(
            max_turns=2,
            stopping_criterion={
                "type": "judge",
                "model": "gpt-judge",
                "question": "Satisfied?",
            },
        ) as a:
            trace = await a.run(_case(), _variant(), None)
    assert trace.extra["user_simulator"]["turns"] == 2
    assert trace.extra["user_simulator"]["stop_reason"] == "max_turns_reached"


# ---------- trace shape + composition ----------


async def test_conversation_lands_in_trace_messages() -> None:
    _StubInner.next_replies = ["assistant turn 1", "assistant turn 2", "final"]
    user = _ScriptedBackend(["second user msg", "third user msg"])
    with _register_backend("claude", user), _register_inner():
        async with _adapter(stopping_criterion={"type": "turn_count", "n": 3}) as a:
            trace = await a.run(_case(), _variant(), None)

    # Three turns -> three user msgs + three assistant msgs.
    assert [m.role for m in trace.messages] == [
        "user", "assistant",
        "user", "assistant",
        "user", "assistant",
    ]
    assert trace.messages[0].content == "Hello, what can you do?"
    assert trace.messages[2].content == "second user msg"
    assert trace.messages[5].content == "final"
    assert trace.output.final_answer == "final"


async def test_aggregates_metrics_and_tool_calls_across_turns() -> None:
    _StubInner.next_replies = ["r1", "r2"]
    user = _ScriptedBackend(["u2"])
    with _register_backend("claude", user), _register_inner():
        async with _adapter(stopping_criterion={"type": "turn_count", "n": 2}) as a:
            trace = await a.run(_case(), _variant(), None)

    # 2 inner turns x (10 in, 20 out, 0.01 cost) + 1 user turn x (1 in, 2 out).
    assert trace.metrics.token_input == 21
    assert trace.metrics.token_output == 42
    assert trace.metrics.cost_usd == pytest.approx(0.02)
    # Tool calls from each inner turn accumulate.
    assert len(trace.tool_calls) == 2


async def test_inner_adapter_composition_used_for_each_turn() -> None:
    """Composition: per-turn synth case carries the rolling conversation; the
    inner adapter is built exactly once and called once per turn."""
    _StubInner.next_replies = ["r1", "r2", "r3"]
    user = _ScriptedBackend(["u2", "u3"])
    with _register_backend("claude", user), _register_inner():
        async with _adapter(stopping_criterion={"type": "turn_count", "n": 3}):
            adapter = _adapter(stopping_criterion={"type": "turn_count", "n": 3})
            async with adapter as a:
                await a.run(_case(), _variant(), None)

    # Inner adapter was constructed twice (one per _adapter() call) — both
    # via the factory, proving composition.
    assert len(_StubInner.seen_inits) == 2
    for init in _StubInner.seen_inits:
        # Outer adapter forwards its own `name` to the inner; no leakage of
        # adapter-type into the case stream.
        assert init["name"] == "user_simulator"
    # Per-turn cases see a growing conversation: 0 msgs before turn 1, 2 msgs
    # before turn 2, 4 msgs before turn 3 (each prior turn appends user +
    # assistant).
    convo_lens = [c["conversation_len"] for c in _StubInner.seen_cases[-3:]]
    assert convo_lens == [1, 3, 5]


def test_factory_registers_user_simulator() -> None:
    assert "user_simulator" in system_adapter_factory.registry.names()


async def test_unknown_user_model_raises_configerror() -> None:
    with _register_inner(), pytest.raises(ConfigError, match="no backend registered"):
        UserSimulatorSystemAdapter(
            user_model="zzz-unknown",
            user_persona_prompt="x",
            stopping_criterion={"type": "turn_count", "n": 1},
            inner_system={"adapter": "stub_inner"},
        )
