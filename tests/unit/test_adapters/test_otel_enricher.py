"""OtelTraceEnricher tests.

Two seams keep the test suite hermetic:

  - A `respx`-mocked `httpx.AsyncClient` plays the OTel query backend.
    No real network.
  - A `RecordingClock` plays `asyncio.sleep`. No `time.sleep`, no wall
    time. Tests assert how many ingestion-lag pauses happened and how
    long each was.

The `[otel]` extra technically isn't required for this enricher
(httpx + jsonpath-ng are core), but the bead spec asks for `importorskip`
so we surface the install hint coherently with the rest of the OTel
triplet. Skipping cleanly is a defence in depth — if a future change
takes a hard dep on the SDK, the test stays well-behaved.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

pytest.importorskip("opentelemetry.sdk.trace")

from eval_harness.adapters.enricher.otel_enricher import OtelTraceEnricher
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import Trace, TraceMetrics, TraceOutput
from eval_harness.core.time import utc_now


class RecordingClock:
    """Deterministic async-sleep replacement. Records every (seconds) call
    so tests can assert ingestion-lag retry behaviour without wall time."""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)


def _trace_with_id(trace_id: str | None) -> Trace:
    now = utc_now()
    extra: dict[str, Any] = {}
    if trace_id is not None:
        extra["trace_id"] = trace_id
    return Trace(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=now,
        finished_at=now,
        latency_ms=0,
        input={"q": "ping"},
        output=TraceOutput(final_answer="pong"),
        metrics=TraceMetrics(),
        extra=extra,
    )


@pytest.fixture
def respx_route() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


# ---- validate_config ----------------------------------------------------


def test_missing_endpoint_raises() -> None:
    with pytest.raises(ConfigError, match="endpoint"):
        OtelTraceEnricher()


def test_endpoint_must_contain_placeholder() -> None:
    with pytest.raises(ConfigError, match="trace_id"):
        OtelTraceEnricher(endpoint="http://x/api/traces/some-fixed-id")


def test_max_attempts_must_be_positive() -> None:
    with pytest.raises(ConfigError, match="max_attempts"):
        OtelTraceEnricher(
            endpoint="http://x/api/traces/{trace_id}", max_attempts=0
        )


def test_negative_wait_for_ingestion_raises() -> None:
    with pytest.raises(ConfigError, match="wait_for_ingestion"):
        OtelTraceEnricher(
            endpoint="http://x/api/traces/{trace_id}",
            wait_for_ingestion_seconds=-1,
        )


def test_bad_jsonpath_in_merge_raises_at_init() -> None:
    """Merge expressions are compiled at __init__ so a typo fails at plan
    time, not on the first cell."""
    with pytest.raises(ConfigError, match="merge"):
        OtelTraceEnricher(
            endpoint="http://x/api/traces/{trace_id}",
            merge={"metrics.token_input": "not a [valid] jsonpath"},
        )


# ---- successful enrichment ---------------------------------------------


async def test_enrich_merges_fields_into_trace(respx_route: respx.MockRouter) -> None:
    respx_route.get("http://tempo:3200/api/traces/abc-123").mock(
        return_value=httpx.Response(
            200,
            json={
                "usage": {"input": 123, "output": 45, "cost": 0.0042},
                "messages": [
                    {"role": "user", "content": "ping"},
                    {"role": "assistant", "content": "pong"},
                ],
            },
        )
    )
    clock = RecordingClock()
    enricher = OtelTraceEnricher(
        endpoint="http://tempo:3200/api/traces/{trace_id}",
        merge={
            "metrics.token_input": "$.usage.input",
            "metrics.token_output": "$.usage.output",
            "metrics.cost_usd": "$.usage.cost",
        },
        _sleep=clock.sleep,
    )
    trace = _trace_with_id("abc-123")
    async with enricher:
        out = await enricher.enrich(trace)

    assert out.metrics.token_input == 123
    assert out.metrics.token_output == 45
    assert out.metrics.cost_usd == pytest.approx(0.0042)
    # First attempt succeeded; no ingestion-lag sleeps occurred.
    assert clock.sleeps == []


async def test_enrich_warns_when_jsonpath_misses(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://tempo:3200/api/traces/abc-123").mock(
        return_value=httpx.Response(200, json={"observations": []})
    )
    clock = RecordingClock()
    enricher = OtelTraceEnricher(
        endpoint="http://tempo:3200/api/traces/{trace_id}",
        merge={"metrics.token_input": "$.usage.input"},
        _sleep=clock.sleep,
    )
    trace = _trace_with_id("abc-123")
    async with enricher:
        out = await enricher.enrich(trace)

    # Token count stayed unset; the miss is recorded on extra.
    assert out.metrics.token_input is None
    warnings = out.extra.get("enrichment_warnings", [])
    assert len(warnings) == 1
    assert warnings[0]["target"] == "metrics.token_input"
    assert "matched nothing" in warnings[0]["reason"]


# ---- ingestion-lag bounded retry ---------------------------------------


async def test_ingestion_lag_retries_with_deterministic_clock(
    respx_route: respx.MockRouter,
) -> None:
    """404 -> wait -> 404 -> wait -> 200. Three GETs, two ingestion-lag
    sleeps of exactly the configured duration. Deterministic; no wall time."""
    responses = [
        httpx.Response(404),
        httpx.Response(404),
        httpx.Response(200, json={"usage": {"input": 7}}),
    ]
    route = respx_route.get("http://tempo:3200/api/traces/abc-123").mock(
        side_effect=responses
    )
    clock = RecordingClock()
    enricher = OtelTraceEnricher(
        endpoint="http://tempo:3200/api/traces/{trace_id}",
        wait_for_ingestion_seconds=2.5,
        max_attempts=5,
        merge={"metrics.token_input": "$.usage.input"},
        _sleep=clock.sleep,
    )
    trace = _trace_with_id("abc-123")
    async with enricher:
        out = await enricher.enrich(trace)

    assert out.metrics.token_input == 7
    assert route.call_count == 3
    # Two sleeps (between attempts 1->2 and 2->3); each exactly the
    # configured duration. No sleep after the final success.
    assert clock.sleeps == [2.5, 2.5]


async def test_max_attempts_exhausted_raises(
    respx_route: respx.MockRouter,
) -> None:
    """Every attempt returns 404 -> AdapterError after max_attempts; the
    runner's failure-soft hook turns it into trace.extra.enrichment_errors,
    but the enricher itself raises so that hook can do its job."""
    route = respx_route.get("http://tempo:3200/api/traces/missing").mock(
        return_value=httpx.Response(404)
    )
    clock = RecordingClock()
    enricher = OtelTraceEnricher(
        endpoint="http://tempo:3200/api/traces/{trace_id}",
        wait_for_ingestion_seconds=0.5,
        max_attempts=3,
        merge={"metrics.token_input": "$.usage.input"},
        _sleep=clock.sleep,
    )
    trace = _trace_with_id("missing")
    async with enricher:
        with pytest.raises(AdapterError, match="gave up after 3 attempts"):
            await enricher.enrich(trace)

    assert route.call_count == 3
    # max_attempts - 1 sleeps; the final attempt doesn't pause before raising.
    assert clock.sleeps == [0.5, 0.5]


async def test_network_error_retried_then_succeeds(
    respx_route: respx.MockRouter,
) -> None:
    """Transient HTTP errors during a poll also retry up to max_attempts.
    The deterministic clock keeps the test fast and reproducible."""
    responses: list[Any] = [
        httpx.ConnectError("conn refused"),
        httpx.Response(200, json={"usage": {"input": 1}}),
    ]
    route = respx_route.get("http://tempo:3200/api/traces/intermittent").mock(
        side_effect=responses
    )
    clock = RecordingClock()
    enricher = OtelTraceEnricher(
        endpoint="http://tempo:3200/api/traces/{trace_id}",
        wait_for_ingestion_seconds=1.0,
        max_attempts=3,
        merge={"metrics.token_input": "$.usage.input"},
        _sleep=clock.sleep,
    )
    trace = _trace_with_id("intermittent")
    async with enricher:
        out = await enricher.enrich(trace)
    assert out.metrics.token_input == 1
    assert route.call_count == 2
    assert clock.sleeps == [1.0]


# ---- missing trace_id ---------------------------------------------------


async def test_missing_trace_id_raises_adapter_error() -> None:
    """No trace.extra['trace_id'] -> AdapterError. The runner's failure-soft
    hook surfaces this as enrichment_errors without aborting the cell."""
    enricher = OtelTraceEnricher(
        endpoint="http://tempo:3200/api/traces/{trace_id}",
    )
    trace = _trace_with_id(None)
    async with enricher:
        with pytest.raises(AdapterError, match="trace_id"):
            await enricher.enrich(trace)


# ---- factory registration -----------------------------------------------


def test_factory_registers_otel_enricher() -> None:
    from eval_harness.factories import trace_enricher_factory

    trace_enricher_factory.load_entry_points()
    assert "otel" in trace_enricher_factory.registry.names()
