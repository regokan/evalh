from __future__ import annotations

import asyncio
import importlib
import inspect
from types import TracebackType
from typing import Any, Self

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import (
    EvalCase,
    RunVariant,
    ToolCall,
    ToolResult,
    Trace,
    TraceMessage,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.core.time import utc_now


class PythonFunctionAdapter:
    name: str

    def __init__(
        self,
        name: str = "python_function",
        *,
        target: str | None = None,
        init_kwargs: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        if not target:
            raise ConfigError("python_function adapter requires 'target'")
        if ":" not in target:
            raise ConfigError(
                f"python_function adapter: 'target' must be of the form "
                f"'module.path:callable', got {target!r}"
            )
        self.name = name
        self._target_spec = target
        self._init_kwargs = dict(init_kwargs or {})
        self._target: Any = None

    async def __aenter__(self) -> Self:
        module_path, _, attr = self._target_spec.partition(":")
        try:
            module = importlib.import_module(module_path)
        except ImportError as e:
            raise ConfigError(
                f"python_function adapter: cannot import module '{module_path}': {e}"
            ) from e
        try:
            obj = getattr(module, attr)
        except AttributeError as e:
            raise ConfigError(
                f"python_function adapter: module '{module_path}' has no attribute "
                f"'{attr}'"
            ) from e

        if self._init_kwargs:
            if not callable(obj):
                raise ConfigError(
                    f"python_function adapter: 'init_kwargs' given but "
                    f"'{self._target_spec}' is not callable"
                )
            obj = obj(**self._init_kwargs)

        self._target = obj
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._target = None

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        target = self._target
        if target is None:
            raise AdapterError(
                "PythonFunctionAdapter.run called outside of an `async with` context"
            )
        if not callable(target):
            raise AdapterError(
                f"python_function adapter: resolved target '{self._target_spec}' is not "
                f"callable"
            )

        case_arg = case.model_dump()
        variant_arg = variant.model_dump()
        if workspace is not None:
            variant_arg["_workspace_path"] = str(workspace.path)

        started_at = utc_now()
        try:
            if inspect.iscoroutinefunction(target):
                result = await target(case_arg, variant_arg)
            else:
                maybe = target(case_arg, variant_arg)
                if inspect.isawaitable(maybe):
                    result = await maybe
                else:
                    result = await asyncio.to_thread(target, case_arg, variant_arg)
        except Exception as e:
            raise AdapterError(
                f"python_function adapter '{self.name}': target raised {type(e).__name__}: {e}"
            ) from e
        finished_at = utc_now()
        latency_ms = max(int((finished_at - started_at).total_seconds() * 1000), 0)

        if not isinstance(result, dict):
            raise AdapterError(
                f"python_function adapter '{self.name}': target must return a dict, "
                f"got {type(result).__name__}"
            )

        return _compose_trace(
            result=result,
            case=case,
            variant=variant,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
        )


def _compose_trace(
    *,
    result: dict[str, Any],
    case: EvalCase,
    variant: RunVariant,
    started_at: Any,
    finished_at: Any,
    latency_ms: int,
) -> Trace:
    final_answer = result.get("final_answer")
    thinking = result.get("thinking")
    if final_answer is not None and not isinstance(final_answer, str):
        final_answer = str(final_answer)
    if thinking is not None and not isinstance(thinking, str):
        thinking = str(thinking)

    tool_calls = [_build_tool_call(t) for t in result.get("tool_calls", []) or []]
    tool_results = [_build_tool_result(t) for t in result.get("tool_results", []) or []]
    messages = [_build_message(m) for m in result.get("messages", []) or []]

    metrics_dict = result.get("metrics") or {}
    metrics = _build_metrics(metrics_dict)

    extra: dict[str, Any] = {}
    raw_extra = result.get("extra")
    if isinstance(raw_extra, dict):
        extra.update(raw_extra)

    return Trace(
        run_id="",
        case_id=case.id,
        variant_name=variant.name,
        started_at=started_at,
        finished_at=finished_at,
        latency_ms=latency_ms,
        input=dict(case.input),
        output=TraceOutput(final_answer=final_answer, thinking=thinking),
        messages=messages,
        tool_calls=tool_calls,
        tool_results=tool_results,
        metrics=metrics,
        extra=extra,
    )


def _build_tool_call(raw: Any) -> ToolCall:
    if isinstance(raw, ToolCall):
        return raw
    if not isinstance(raw, dict):
        return ToolCall(name=str(raw), arguments={})
    return ToolCall(
        id=raw.get("id"),
        name=str(raw.get("name", "")),
        arguments=dict(raw.get("arguments") or raw.get("input") or {}),
    )


def _build_tool_result(raw: Any) -> ToolResult:
    if isinstance(raw, ToolResult):
        return raw
    if not isinstance(raw, dict):
        return ToolResult(name="", content=str(raw))
    content = raw.get("content", "")
    if not isinstance(content, dict | str):
        content = str(content)
    return ToolResult(
        tool_call_id=raw.get("tool_call_id"),
        name=str(raw.get("name", "")),
        content=content,
    )


def _build_message(raw: Any) -> TraceMessage:
    if isinstance(raw, TraceMessage):
        return raw
    if not isinstance(raw, dict):
        return TraceMessage(role="assistant", content=str(raw))
    return TraceMessage(
        role=str(raw.get("role", "assistant")),
        content=raw.get("content"),
        thinking=raw.get("thinking"),
        name=raw.get("name"),
    )


def _build_metrics(metrics_dict: dict[str, Any]) -> TraceMetrics:
    return TraceMetrics(
        token_input=_int_or_none(metrics_dict.get("token_input")),
        token_output=_int_or_none(metrics_dict.get("token_output")),
        token_thinking=_int_or_none(metrics_dict.get("token_thinking")),
        cost_usd=_float_or_none(metrics_dict.get("cost_usd")),
        cost_thinking_usd=_float_or_none(metrics_dict.get("cost_thinking_usd")),
        latency_first_token_ms=_int_or_none(metrics_dict.get("latency_first_token_ms")),
        latency_last_token_ms=_int_or_none(metrics_dict.get("latency_last_token_ms")),
        tokens_per_second=_float_or_none(metrics_dict.get("tokens_per_second")),
        stream_chunks=_int_or_none(metrics_dict.get("stream_chunks")),
        stream_completed=_bool_or_none(metrics_dict.get("stream_completed")),
        custom=dict(metrics_dict.get("custom") or {}),
    )


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    return None
