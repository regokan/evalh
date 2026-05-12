from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eval_harness.adapters.system.replay_adapter import ReplaySystemAdapter
from eval_harness.core.errors import AdapterError
from eval_harness.core.models import (
    EvalCase,
    RunVariant,
    ToolCall,
    Trace,
    TraceMessage,
    TraceMetrics,
    TraceOutput,
)

_ORIG_START = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)
_ORIG_END = datetime(2026, 5, 1, 10, 0, 3, 142_000, tzinfo=UTC)
_ORIG_LATENCY_MS = 3142


def _embedded_trace() -> Trace:
    return Trace(
        run_id="original_production_run",
        case_id="prod_001",
        variant_name="prod",
        started_at=_ORIG_START,
        finished_at=_ORIG_END,
        latency_ms=_ORIG_LATENCY_MS,
        input={"user_message": "What is in ABC123?"},
        output=TraceOutput(
            final_answer="Richmond, $1.35M",
            thinking="step by step thinking text",
        ),
        messages=[TraceMessage(role="assistant", content="Richmond, $1.35M")],
        tool_calls=[ToolCall(id="call_1", name="lookup", arguments={"q": "ABC123"})],
        metrics=TraceMetrics(
            token_input=120,
            token_output=85,
            token_thinking=40,
            cost_usd=0.0123,
            cost_thinking_usd=0.0030,
            latency_first_token_ms=210,
            latency_last_token_ms=3100,
            tokens_per_second=27.4,
            stream_chunks=18,
            stream_completed=True,
            custom={"upstream_score": 4.2},
        ),
        extra={"trace_id": "lf_abc123"},
    )


def _case_with_trace(trace: Trace | None = None) -> EvalCase:
    case = EvalCase(id="c_now", input={"user_message": "what is ABC123?"})
    case._embedded_trace = trace if trace is not None else _embedded_trace()
    return case


def _variant(name: str = "production_replay") -> RunVariant:
    return RunVariant(name=name, adapter="replay", config={})


async def test_unwraps_embedded_trace() -> None:
    case = _case_with_trace()
    adapter = ReplaySystemAdapter()
    async with adapter:
        replayed = await adapter.run(case, _variant(), None)
    assert isinstance(replayed, Trace)
    assert replayed.output.final_answer == "Richmond, $1.35M"
    assert replayed.output.thinking == "step by step thinking text"
    assert replayed.messages[0].content == "Richmond, $1.35M"
    assert replayed.tool_calls[0].name == "lookup"


async def test_preserves_original_timestamps() -> None:
    """CRITICAL: replay traces keep the original wall-clock byte-for-byte."""
    case = _case_with_trace()
    adapter = ReplaySystemAdapter()
    async with adapter:
        replayed = await adapter.run(case, _variant(), None)

    assert replayed.started_at == _ORIG_START
    assert replayed.finished_at == _ORIG_END
    assert replayed.latency_ms == _ORIG_LATENCY_MS


async def test_preserves_original_metrics() -> None:
    case = _case_with_trace()
    adapter = ReplaySystemAdapter()
    async with adapter:
        replayed = await adapter.run(case, _variant(), None)

    original = case._embedded_trace.metrics
    assert replayed.metrics.model_dump() == original.model_dump()


async def test_sets_replay_provenance_in_extra() -> None:
    case = _case_with_trace()
    adapter = ReplaySystemAdapter(
        metadata={"source": "langfuse-production"}
    )
    async with adapter:
        replayed = await adapter.run(case, _variant(), None)

    assert replayed.extra["source"] == "replay"
    rf = replayed.extra["replayed_from"]
    assert rf["platform"] == "langfuse-production"
    assert rf["trace_id"] == "lf_abc123"
    assert "fetched_at" in rf
    # Upstream trace_id key still present too.
    assert replayed.extra["trace_id"] == "lf_abc123"


async def test_no_embedded_trace_raises_adapter_error() -> None:
    case = EvalCase(id="c1", input={})
    adapter = ReplaySystemAdapter()
    async with adapter:
        with pytest.raises(AdapterError):
            await adapter.run(case, _variant(), None)


async def test_run_id_overrides_to_current() -> None:
    """case_id and variant_name swap to the current run's join keys (run_id is
    finalised by the runner — adapter just clears any upstream value)."""
    case = _case_with_trace()
    case_id_for_join = "c_replay_001"
    case = EvalCase(id=case_id_for_join, input={})
    case._embedded_trace = _embedded_trace()

    adapter = ReplaySystemAdapter()
    async with adapter:
        replayed = await adapter.run(case, _variant("candidate_v2"), None)

    assert replayed.case_id == case_id_for_join
    assert replayed.variant_name == "candidate_v2"


async def test_does_not_mutate_source_trace() -> None:
    """Re-running the adapter on the same case must work — i.e. the adapter
    must not mutate `case._embedded_trace`."""
    case = _case_with_trace()
    snapshot_extra = dict(case._embedded_trace.extra)
    snapshot_case_id = case._embedded_trace.case_id

    adapter = ReplaySystemAdapter()
    async with adapter:
        await adapter.run(case, _variant("v1"), None)
        # Second call: original trace should still be pristine.
        second = await adapter.run(case, _variant("v2"), None)

    assert case._embedded_trace.extra == snapshot_extra
    assert case._embedded_trace.case_id == snapshot_case_id
    # Second replay still works.
    assert second.extra["source"] == "replay"


def test_factory_registers_replay_adapter() -> None:
    from eval_harness.factories import system_adapter_factory

    assert "replay" in system_adapter_factory.registry.names()
