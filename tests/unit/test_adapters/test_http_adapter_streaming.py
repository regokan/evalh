from __future__ import annotations

import contextlib
from collections.abc import Iterator

import httpx
import pytest
import respx

from eval_harness.adapters.system.http_adapter import HttpSystemAdapter
from eval_harness.core.models import EvalCase, ExpectedBehavior, RunVariant


def _case() -> EvalCase:
    return EvalCase(
        id="c1",
        input={"user_message": "hi"},
        metadata={},
        expected=ExpectedBehavior(),
    )


def _variant() -> RunVariant:
    return RunVariant(name="v1", adapter="http", config={})


def _fake_clock(ticks: list[float]) -> FakeClock:
    return FakeClock(ticks)


class FakeClock:
    """Deterministic monotonic clock. Returns the next tick on each call."""

    def __init__(self, ticks: list[float]) -> None:
        self._ticks: Iterator[float] = iter(ticks)
        self._last = 0.0

    def __call__(self) -> float:
        with contextlib.suppress(StopIteration):
            self._last = next(self._ticks)
        return self._last


_SSE_BODY = (
    b"data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}\n"
    b"data: {\"choices\":[{\"delta\":{\"content\":\" world\"}}]}\n"
    b"data: {\"choices\":[{\"delta\":{\"content\":\"!\"}}]}\n"
    b"data: {\"choices\":[{\"finish_reason\":\"stop\"}]}\n"
    b"data: [DONE]\n"
)


def _make_adapter(clock: FakeClock) -> HttpSystemAdapter:
    return HttpSystemAdapter(
        name="streamy",
        endpoint="https://api.example.com/chat",
        stream=True,
        stream_format="sse",
        stream_event_field="$.choices[0].delta.content",
        stream_done_field="$.choices[0].finish_reason",
        clock=clock,
    )


@respx.mock
async def test_first_token_time_recorded() -> None:
    # Clock progression: t0 (start) = 1.0, first event = 1.25, then 1.5, 1.75, 2.0, 2.0
    clock = _fake_clock([1.0, 1.25, 1.5, 1.75, 2.0, 2.0])
    respx.post("https://api.example.com/chat").mock(
        return_value=httpx.Response(200, content=_SSE_BODY)
    )
    adapter = _make_adapter(clock)
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    assert trace.metrics.latency_first_token_ms == 250  # (1.25 - 1.0) * 1000


@respx.mock
async def test_last_token_time_records_total_wall() -> None:
    clock = _fake_clock([10.0, 10.1, 10.3, 10.6, 11.0, 11.0])
    respx.post("https://api.example.com/chat").mock(
        return_value=httpx.Response(200, content=_SSE_BODY)
    )
    adapter = _make_adapter(clock)
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    # Last event timestamp was 11.0; t0 was 10.0 → 1000ms wall time.
    assert trace.metrics.latency_last_token_ms == 1000


@respx.mock
async def test_tokens_per_second_computed() -> None:
    # 3 content tokens + 1 finish-reason event + 1 DONE = 5 chunks; only 3 carry
    # tokens. Stream wall = 2.0s. tps = 3 / 2.0 = 1.5.
    clock = _fake_clock([0.0, 0.5, 1.0, 1.5, 2.0, 2.0])
    respx.post("https://api.example.com/chat").mock(
        return_value=httpx.Response(200, content=_SSE_BODY)
    )
    adapter = _make_adapter(clock)
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    assert trace.metrics.tokens_per_second == pytest.approx(1.5)
    assert trace.metrics.token_output == 3
    assert trace.output.final_answer == "Hello world!"


@respx.mock
async def test_stream_chunks_counted() -> None:
    clock = _fake_clock([0.0, 0.1, 0.2, 0.3, 0.4, 0.4])
    respx.post("https://api.example.com/chat").mock(
        return_value=httpx.Response(200, content=_SSE_BODY)
    )
    adapter = _make_adapter(clock)
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    # 3 content events + 1 finish-reason event = 4. The done signal on the
    # finish-reason event halts iteration before the `[DONE]` sentinel is read.
    assert trace.metrics.stream_chunks == 4


@respx.mock
async def test_stream_completed_set_on_done_signal() -> None:
    clock = _fake_clock([0.0, 0.1, 0.2, 0.3, 0.4, 0.4])
    respx.post("https://api.example.com/chat").mock(
        return_value=httpx.Response(200, content=_SSE_BODY)
    )
    adapter = _make_adapter(clock)
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    assert trace.metrics.stream_completed is True


@respx.mock
async def test_partial_stream_records_stream_completed_false_and_keeps_partial_final_answer() -> None:
    # No done signal, no [DONE] sentinel — just two content events. The
    # adapter should report stream_completed=False and still aggregate the
    # tokens it saw into final_answer.
    partial = (
        b"data: {\"choices\":[{\"delta\":{\"content\":\"part-\"}}]}\n"
        b"data: {\"choices\":[{\"delta\":{\"content\":\"one\"}}]}\n"
    )
    clock = _fake_clock([0.0, 0.2, 0.5, 0.5])
    respx.post("https://api.example.com/chat").mock(
        return_value=httpx.Response(200, content=partial)
    )
    adapter = _make_adapter(clock)
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    assert trace.metrics.stream_completed is False
    assert trace.output.final_answer == "part-one"
    assert trace.metrics.stream_chunks == 2
