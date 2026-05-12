"""UserSimulatorSystemAdapter — multi-turn conversations with a simulated user.

The user simulator IS a SystemAdapter (no new family). It composes with an
``inner_system`` configured exactly like any other SystemAdapter (http,
python_function, cli, …) — the wrapper drives the turn loop, the inner
adapter does the actual system call. The user role is played by an LLM
resolved through the shared ``LlmBackend`` registry.

See docs/Adapters.md > "v1: user_simulator".
"""

from __future__ import annotations

import contextlib
import re
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.llm_backends import LlmBackend, llm_backend_registry
from eval_harness.core.models import (
    EvalCase,
    RunVariant,
    Trace,
    TraceMessage,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.core.time import utc_now

if TYPE_CHECKING:
    from eval_harness.adapters.system.base import SystemAdapter

_VALID_STOP_TYPES = {"turn_count", "content_match", "judge"}
_USER_MESSAGE_KEY = "user_message"
_JUDGE_AFFIRMATIVE = re.compile(r"\b(yes|true|stop|satisfied|done|complete)\b", re.IGNORECASE)
_DEFAULT_USER_MAX_TOKENS = 512
_DEFAULT_JUDGE_MAX_TOKENS = 32


class UserSimulatorSystemAdapter:
    name: str

    def __init__(
        self,
        name: str = "user_simulator",
        *,
        user_model: str | None = None,
        user_persona_prompt: str | None = None,
        max_turns: int = 10,
        stopping_criterion: dict[str, Any] | None = None,
        inner_system: dict[str, Any] | None = None,
        cost_limit_usd: float | None = None,
        user_max_tokens: int = _DEFAULT_USER_MAX_TOKENS,
        metadata: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        if not user_model:
            raise ConfigError(
                "user_simulator: 'user_model' (str) is required"
            )
        if not user_persona_prompt:
            raise ConfigError(
                "user_simulator: 'user_persona_prompt' (str) is required"
            )
        if max_turns <= 0:
            raise ConfigError(
                f"user_simulator: 'max_turns' must be > 0, got {max_turns}"
            )
        _validate_stopping(stopping_criterion)
        if not isinstance(inner_system, dict) or not inner_system:
            raise ConfigError(
                "user_simulator: 'inner_system' dict is required"
            )
        if "adapter" not in inner_system or not isinstance(
            inner_system["adapter"], str
        ):
            raise ConfigError(
                "user_simulator: inner_system.adapter (str) is required"
            )

        self.name = name
        self._user_model: str = user_model
        self._persona: str = user_persona_prompt
        self._max_turns = int(max_turns)
        # _validate_stopping has already type-checked the dict.
        assert stopping_criterion is not None
        self._stop: dict[str, Any] = dict(stopping_criterion)
        self._inner_cfg: dict[str, Any] = dict(inner_system)
        self._cost_limit_usd = cost_limit_usd
        self._user_max_tokens = int(user_max_tokens)
        self._metadata = dict(metadata or {})

        # Plan-time backend resolution so a missing SDK / unknown model fails
        # before any case runs.
        self._user_backend: LlmBackend = llm_backend_registry.resolve(user_model)
        if self._stop["type"] == "judge":
            self._judge_model: str = str(self._stop["model"])
            self._judge_backend: LlmBackend | None = llm_backend_registry.resolve(
                self._judge_model
            )
        else:
            self._judge_model = ""
            self._judge_backend = None

        # Build the inner adapter at plan time so configuration errors there
        # surface immediately instead of after the first turn.
        self._inner: SystemAdapter = _build_inner_adapter(
            self._inner_cfg, outer_name=self.name
        )
        self._exit_stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        stack = contextlib.AsyncExitStack()
        try:
            await stack.enter_async_context(self._inner)
        except BaseException:
            await stack.aclose()
            raise
        self._exit_stack = stack
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        if self._exit_stack is None:
            raise AdapterError(
                "user_simulator: run() called outside of `async with` context"
            )

        started_at = utc_now()
        initial = case.input.get(_USER_MESSAGE_KEY)
        if not isinstance(initial, str) or not initial:
            raise AdapterError(
                "user_simulator: case.input.user_message (non-empty str) is "
                "required; got "
                f"{initial!r}"
            )

        conversation: list[TraceMessage] = []
        aggregated_tool_calls: list[Any] = []
        aggregated_tool_results: list[Any] = []
        aggregated_thinking: list[str] = []
        sum_in = sum_out = sum_thinking = 0
        sum_cost = 0.0
        cost_seen = False

        current_user_msg = initial
        last_assistant_text: str | None = None
        stop_reason: str | None = None
        turns_executed = 0

        for turn in range(self._max_turns):
            conversation.append(TraceMessage(role="user", content=current_user_msg))
            # Synthesise a per-turn case carrying the latest user message and the
            # running transcript in `conversation`. Inner adapters that only
            # know about `user_message` keep working; multi-turn-aware inner
            # adapters can read `conversation` from input.
            inner_input = dict(case.input)
            inner_input[_USER_MESSAGE_KEY] = current_user_msg
            inner_input["conversation"] = [m.model_dump(mode="json") for m in conversation]
            inner_case = case.model_copy(update={"input": inner_input})

            inner_trace = await self._inner.run(inner_case, variant, workspace)

            last_assistant_text = inner_trace.output.final_answer or ""
            conversation.append(
                TraceMessage(
                    role="assistant",
                    content=last_assistant_text,
                    thinking=inner_trace.output.thinking,
                )
            )
            if inner_trace.output.thinking:
                aggregated_thinking.append(inner_trace.output.thinking)
            aggregated_tool_calls.extend(inner_trace.tool_calls)
            aggregated_tool_results.extend(inner_trace.tool_results)
            sum_in += inner_trace.metrics.token_input or 0
            sum_out += inner_trace.metrics.token_output or 0
            sum_thinking += inner_trace.metrics.token_thinking or 0
            if inner_trace.metrics.cost_usd is not None:
                sum_cost += inner_trace.metrics.cost_usd
                cost_seen = True

            turns_executed = turn + 1

            stop_reason = await self._check_stop(
                turn_index=turn + 1, conversation=conversation
            )
            if stop_reason is not None:
                break
            if turn + 1 >= self._max_turns:
                stop_reason = "max_turns_reached"
                break

            # Generate the next user turn.
            next_user_msg, user_call_in, user_call_out = await self._next_user_turn(
                conversation
            )
            sum_in += user_call_in
            sum_out += user_call_out
            current_user_msg = next_user_msg

        finished_at = utc_now()
        latency_ms = max(0, int((finished_at - started_at).total_seconds() * 1000))

        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
            input=dict(case.input),
            output=TraceOutput(
                final_answer=last_assistant_text or "",
                thinking="\n\n".join(aggregated_thinking) or None,
            ),
            messages=conversation,
            tool_calls=aggregated_tool_calls,
            tool_results=aggregated_tool_results,
            metrics=TraceMetrics(
                token_input=sum_in or None,
                token_output=sum_out or None,
                token_thinking=sum_thinking or None,
                cost_usd=sum_cost if cost_seen else None,
            ),
            extra={
                "user_simulator": {
                    "turns": turns_executed,
                    "stop_reason": stop_reason,
                    "user_model": self._user_model,
                    "max_turns": self._max_turns,
                }
            },
        )

    async def _next_user_turn(
        self, conversation: list[TraceMessage]
    ) -> tuple[str, int, int]:
        prompt = _render_conversation_for_user(conversation)
        call = await self._user_backend.generate(
            prompt,
            model=self._user_model,
            max_tokens=self._user_max_tokens,
            system=self._persona,
            cost_limit_usd=self._cost_limit_usd,
        )
        text = (call.text or "").strip()
        if not text and isinstance(call.structured, dict):
            text = str(call.structured.get("message") or call.structured.get("text") or "")
        if not text:
            raise AdapterError(
                "user_simulator: user_backend returned an empty next-user turn"
            )
        return text, call.token_input, call.token_output

    async def _check_stop(
        self, *, turn_index: int, conversation: list[TraceMessage]
    ) -> str | None:
        stop_type = self._stop["type"]
        if stop_type == "turn_count":
            n = int(self._stop["n"])
            if turn_index >= n:
                return f"turn_count(n={n})"
            return None
        if stop_type == "content_match":
            latest = conversation[-1].content if conversation else None
            if not isinstance(latest, str):
                return None
            patterns: list[str] = list(self._stop["patterns"])
            case_sensitive = bool(self._stop.get("case_sensitive", False))
            haystack = latest if case_sensitive else latest.lower()
            for pat in patterns:
                needle = pat if case_sensitive else pat.lower()
                if needle in haystack:
                    return f"content_match({pat!r})"
            return None
        if stop_type == "judge":
            assert self._judge_backend is not None
            question: str = self._stop["question"]
            verdict, _, _ = await self._call_judge(question, conversation)
            if _JUDGE_AFFIRMATIVE.search(verdict):
                return f"judge({self._judge_model})"
            return None
        return None  # pragma: no cover — validated in _validate_stopping

    async def _call_judge(
        self, question: str, conversation: list[TraceMessage]
    ) -> tuple[str, int, int]:
        assert self._judge_backend is not None
        rendered = _render_conversation_for_judge(conversation)
        prompt = (
            f"{rendered}\n\n"
            f"Question: {question}\n"
            "Answer with just 'yes' or 'no'."
        )
        call = await self._judge_backend.generate(
            prompt,
            model=self._judge_model,
            max_tokens=_DEFAULT_JUDGE_MAX_TOKENS,
            system="You are a binary judge. Answer only with 'yes' or 'no'.",
        )
        return (call.text or "").strip(), call.token_input, call.token_output


def _validate_stopping(stop: Any) -> None:
    if not isinstance(stop, dict):
        raise ConfigError(
            "user_simulator: 'stopping_criterion' dict is required"
        )
    stop_type = stop.get("type")
    if stop_type not in _VALID_STOP_TYPES:
        raise ConfigError(
            f"user_simulator: stopping_criterion.type must be one of "
            f"{sorted(_VALID_STOP_TYPES)}, got {stop_type!r}"
        )
    if stop_type == "turn_count":
        n = stop.get("n")
        if not isinstance(n, int) or n <= 0:
            raise ConfigError(
                "user_simulator: stopping_criterion.n must be a positive int"
            )
    elif stop_type == "content_match":
        patterns = stop.get("patterns")
        if (
            not isinstance(patterns, list)
            or not patterns
            or not all(isinstance(p, str) and p for p in patterns)
        ):
            raise ConfigError(
                "user_simulator: stopping_criterion.patterns must be a "
                "non-empty list[str]"
            )
    elif stop_type == "judge":
        if not isinstance(stop.get("model"), str) or not stop["model"]:
            raise ConfigError(
                "user_simulator: stopping_criterion.model (str) is required "
                "for type=judge"
            )
        if not isinstance(stop.get("question"), str) or not stop["question"]:
            raise ConfigError(
                "user_simulator: stopping_criterion.question (str) is "
                "required for type=judge"
            )


def _render_conversation_for_user(messages: list[TraceMessage]) -> str:
    """Format the running conversation as a prompt for the user-role LLM.

    The LLM speaks as 'User'; the inner system spoke as 'Assistant'. We
    instruct the model to write only the next User turn.
    """
    lines: list[str] = []
    for m in messages:
        role = "Assistant" if m.role == "assistant" else "User"
        content = m.content if isinstance(m.content, str) else str(m.content)
        lines.append(f"{role}: {content}")
    lines.append("User:")  # the model continues from here
    return "\n".join(lines)


def _render_conversation_for_judge(messages: list[TraceMessage]) -> str:
    lines: list[str] = ["=== Conversation ==="]
    for m in messages:
        role = "Assistant" if m.role == "assistant" else "User"
        content = m.content if isinstance(m.content, str) else str(m.content)
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _build_inner_adapter(
    inner_cfg: dict[str, Any], *, outer_name: str
) -> SystemAdapter:
    from eval_harness.factories import system_adapter_factory

    adapter_type = str(inner_cfg["adapter"])
    variant = RunVariant(
        name=outer_name,
        adapter=adapter_type,
        config={k: v for k, v in inner_cfg.items() if k != "adapter"},
    )
    return system_adapter_factory.build(variant)
