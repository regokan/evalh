"""LangfuseTraceEnricher tests.

Failure-soft behaviour at the *runner* level is tested in ev-sa7's runner
tests; here we pin the adapter-level contract: enrich() raises when the
upstream is unreachable / missing, and merges fields correctly when it's
not.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eval_harness._platforms.langfuse import LangfuseClient
from eval_harness.adapters.enricher.langfuse_enricher import LangfuseTraceEnricher
from eval_harness.core.errors import AdapterError
from eval_harness.core.models import Trace, TraceOutput
from tests.unit.test_platforms.test_langfuse import (
    FakeLangfuseSdk,
    _DeterministicClock,
)

_NOW = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)


def _trace_with(trace_id: str | None) -> Trace:
    return Trace(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=0,
        input={},
        output=TraceOutput(final_answer="initial"),
        extra={"trace_id": trace_id} if trace_id else {},
    )


async def test_enricher_merges_upstream_fields_into_local_trace() -> None:
    sdk = FakeLangfuseSdk()
    sdk.seed_trace(
        "lf_abc",
        {
            "score": 4.5,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "annotations": ["fluent", "concise"],
        },
    )
    client = LangfuseClient(_sdk=sdk)
    enricher = LangfuseTraceEnricher(
        client=client,
        merge={
            "metrics.token_input": "$.usage.input_tokens",
            "metrics.token_output": "$.usage.output_tokens",
            "extra.upstream_score": "$.score",
            "extra.annotations": "$.annotations",
        },
    )
    async with enricher:
        out = await enricher.enrich(_trace_with("lf_abc"))

    assert out.metrics.token_input == 100
    assert out.metrics.token_output == 50
    assert out.extra["upstream_score"] == 4.5
    assert out.extra["annotations"] == ["fluent", "concise"]
    # The unrelated fields stayed put.
    assert out.output.final_answer == "initial"


async def test_enricher_raises_when_trace_id_missing() -> None:
    sdk = FakeLangfuseSdk()
    client = LangfuseClient(_sdk=sdk)
    enricher = LangfuseTraceEnricher(
        client=client, merge={"extra.score": "$.score"}
    )
    async with enricher:
        with pytest.raises(AdapterError, match=r"Trace\.extra\.trace_id"):
            await enricher.enrich(_trace_with(None))


async def test_enricher_raises_when_upstream_never_ingests() -> None:
    sdk = FakeLangfuseSdk()
    sdk.ingest_after_calls["lf_missing"] = 100  # never appears

    clock = _DeterministicClock()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    client = LangfuseClient(_sdk=sdk, clock=clock, sleeper=fake_sleep)
    enricher = LangfuseTraceEnricher(
        client=client,
        merge={"extra.score": "$.score"},
        wait_for_ingestion_seconds=1.0,
    )
    async with enricher:
        with pytest.raises(AdapterError, match="did not appear"):
            await enricher.enrich(_trace_with("lf_missing"))

    # Deterministic: we polled while inside the budget.
    assert len(slept) >= 1


async def test_enricher_waits_through_ingestion_lag_and_then_succeeds() -> None:
    sdk = FakeLangfuseSdk()
    sdk.seed_trace("lf_late", {"score": 4.0})
    sdk.ingest_after_calls["lf_late"] = 2

    clock = _DeterministicClock()

    async def fake_sleep(seconds: float) -> None:
        clock.advance(seconds)

    client = LangfuseClient(_sdk=sdk, clock=clock, sleeper=fake_sleep)
    enricher = LangfuseTraceEnricher(
        client=client,
        merge={"extra.upstream_score": "$.score"},
        wait_for_ingestion_seconds=10.0,
    )
    async with enricher:
        out = await enricher.enrich(_trace_with("lf_late"))

    assert out.extra["upstream_score"] == 4.0
    assert sdk._fetch_calls["lf_late"] == 3


async def test_enricher_invalid_jsonpath_raises_at_enrich_time() -> None:
    sdk = FakeLangfuseSdk()
    sdk.seed_trace("lf_abc", {"score": 1})
    client = LangfuseClient(_sdk=sdk)
    enricher = LangfuseTraceEnricher(
        client=client,
        merge={"extra.bad": "$..[bogus syntax"},
    )
    async with enricher:
        with pytest.raises(AdapterError, match="invalid JSONPath"):
            await enricher.enrich(_trace_with("lf_abc"))


def test_enricher_requires_merge_config() -> None:
    sdk = FakeLangfuseSdk()
    client = LangfuseClient(_sdk=sdk)
    with pytest.raises(AdapterError, match="'merge'"):
        LangfuseTraceEnricher(client=client)


def test_factory_registers_langfuse_enricher() -> None:
    from eval_harness.factories.trace_enricher_factory import TraceEnricherFactory

    f = TraceEnricherFactory()
    f.load_entry_points()
    assert "langfuse" in f.registry.names()


async def test_unmatched_jsonpath_is_a_no_op_not_an_error() -> None:
    """A merge rule pointing at a field the upstream payload doesn't have
    should leave the local trace unchanged; that's the failure-soft posture
    inside the success path."""
    sdk = FakeLangfuseSdk()
    sdk.seed_trace("lf_abc", {"score": 1})
    client = LangfuseClient(_sdk=sdk)
    enricher = LangfuseTraceEnricher(
        client=client,
        merge={
            "extra.found": "$.score",
            "extra.absent": "$.does_not_exist",
        },
    )
    async with enricher:
        out = await enricher.enrich(_trace_with("lf_abc"))
    assert out.extra["found"] == 1
    assert "absent" not in out.extra
