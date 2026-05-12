from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from eval_harness.core.models import (
    EvalCase,
    ExpectedBehavior,
    ToolCall,
    ToolResult,
    Trace,
    TraceOutput,
)

_NOW = datetime(2026, 5, 12, tzinfo=UTC)

TraceFactory = Callable[..., Trace]
CaseFactory = Callable[..., EvalCase]


@pytest.fixture
def make_trace() -> TraceFactory:
    def _build(
        *,
        final_answer: str | None = None,
        thinking: str | None = None,
        tool_calls: list[ToolCall] | None = None,
        tool_results: list[ToolResult] | None = None,
        structured: dict[str, Any] | None = None,
    ) -> Trace:
        return Trace(
            run_id="r1",
            case_id="c1",
            variant_name="v1",
            started_at=_NOW,
            finished_at=_NOW,
            latency_ms=10,
            input={"q": "hi"},
            output=TraceOutput(
                final_answer=final_answer,
                thinking=thinking,
                structured=structured,
            ),
            tool_calls=tool_calls or [],
            tool_results=tool_results or [],
        )

    return _build


@pytest.fixture
def make_case() -> CaseFactory:
    def _build(
        *,
        id: str = "c1",
        metadata: dict[str, Any] | None = None,
        expected: ExpectedBehavior | None = None,
    ) -> EvalCase:
        return EvalCase(
            id=id,
            input={"q": "hi"},
            metadata=metadata or {},
            expected=expected or ExpectedBehavior(),
        )

    return _build
