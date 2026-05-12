from __future__ import annotations

from collections.abc import Callable

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, ToolCall, ToolResult, Trace
from eval_harness.evaluators.tool_called import ToolCalledEvaluator

CaseFactory = Callable[..., EvalCase]
TraceFactory = Callable[..., Trace]


async def test_pass_when_tool_called_with_matching_args(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = ToolCalledEvaluator(
        name="calls_listing",
        tool_name="get_listing",
        args_match={"listing_id": "{{ case.metadata.listing_id }}"},
    )
    case = make_case(metadata={"listing_id": "ABC123"})
    trace = make_trace(
        tool_calls=[
            ToolCall(id="t1", name="get_listing", arguments={"listing_id": "ABC123"})
        ],
        tool_results=[
            ToolResult(tool_call_id="t1", name="get_listing", content={"ok": True})
        ],
    )

    result = await ev.evaluate(case, trace, None)
    assert result.passed
    assert result.detail["count"] == 1
    assert result.detail["rendered_args_match"] == {"listing_id": "ABC123"}
    assert result.started_at <= result.finished_at
    assert result.latency_ms >= 0


async def test_fail_when_tool_errored(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = ToolCalledEvaluator(name="calls_listing", tool_name="get_listing")
    case = make_case()
    trace = make_trace(
        tool_calls=[ToolCall(id="t1", name="get_listing", arguments={})],
        tool_results=[
            ToolResult(tool_call_id="t1", name="get_listing", content={"error": "boom"})
        ],
    )

    result = await ev.evaluate(case, trace, None)
    assert not result.passed
    assert "errored" in result.reason


async def test_fail_when_tool_not_called(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = ToolCalledEvaluator(name="calls_listing", tool_name="get_listing", min_calls=1)
    case = make_case()
    trace = make_trace(tool_calls=[])

    result = await ev.evaluate(case, trace, None)
    assert not result.passed
    assert "< min_calls" in result.reason


async def test_regex_args_match(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = ToolCalledEvaluator(
        name="calls_listing",
        tool_name="get_listing",
        args_match={"listing_id": "~=^[A-Z]{3}\\d+$"},
    )
    case = make_case()
    trace = make_trace(
        tool_calls=[ToolCall(name="get_listing", arguments={"listing_id": "ABC123"})],
    )
    result = await ev.evaluate(case, trace, None)
    assert result.passed


async def test_validate_config_requires_tool_name() -> None:
    with pytest.raises(ConfigError, match="tool_name"):
        ToolCalledEvaluator.validate_config({})
