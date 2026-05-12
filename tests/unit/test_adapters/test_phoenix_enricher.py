"""PhoenixTraceEnricher tests.

Uses a respx-mocked HTTP layer + a deterministic clock (no time.sleep,
no real network) and a PhoenixClient injected directly into the enricher.
Skips when [phoenix] isn't installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

pytest.importorskip("phoenix.otel")

from eval_harness._platforms import otel as otel_mod
from eval_harness._platforms import phoenix as phoenix_mod
from eval_harness._platforms.phoenix import PhoenixClient
from eval_harness.adapters.enricher.phoenix_enricher import PhoenixTraceEnricher
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import Trace, TraceMetrics, TraceOutput
from eval_harness.core.time import utc_now


class _DeterministicClock:
    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds


@pytest.fixture(autouse=True)
def _clean_registries() -> None:
    otel_mod._clear_registry_for_tests()
    phoenix_mod._clear_registry_for_tests()


@pytest.fixture
def respx_route() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


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


# ---- validate_config ----------------------------------------------------


def test_missing_base_url_raises() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        PhoenixTraceEnricher(merge={"messages": "$.messages"})


def test_missing_merge_raises() -> None:
    with pytest.raises(ConfigError, match="merge"):
        PhoenixTraceEnricher(base_url="http://phoenix:6006")


def test_bad_jsonpath_in_merge_raises_at_init() -> None:
    with pytest.raises(ConfigError, match="JSONPath"):
        PhoenixTraceEnricher(
            base_url="http://phoenix:6006",
            merge={"metrics.token_input": "$[invalid jsonpath"},
        )


# ---- happy path ---------------------------------------------------------


async def test_enrich_merges_fields(respx_route: respx.MockRouter) -> None:
    respx_route.get("http://phoenix:6006/v1/traces/abc-123").mock(
        return_value=httpx.Response(
            200,
            json={
                "usage": {"input": 11, "output": 22, "cost": 0.0013},
            },
        )
    )
    clock = _DeterministicClock()
    client = PhoenixClient(
        base_url="http://phoenix:6006",
        clock=clock,
        sleeper=clock.sleep,
    )
    enricher = PhoenixTraceEnricher(
        base_url="http://phoenix:6006",
        merge={
            "metrics.token_input": "$.usage.input",
            "metrics.token_output": "$.usage.output",
            "metrics.cost_usd": "$.usage.cost",
        },
        client=client,
    )
    trace = _trace_with_id("abc-123")
    async with enricher:
        out = await enricher.enrich(trace)

    assert out.metrics.token_input == 11
    assert out.metrics.token_output == 22
    assert out.metrics.cost_usd == pytest.approx(0.0013)
    assert clock.sleeps == []


# ---- ingestion-lag retry ------------------------------------------------


async def test_ingestion_lag_polls_with_deterministic_clock(
    respx_route: respx.MockRouter,
) -> None:
    """404 -> wait -> 404 -> wait -> 200. Three GETs, two deterministic
    sleeps. No wall time."""
    route = respx_route.get("http://phoenix:6006/v1/traces/abc-123").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(404),
            httpx.Response(200, json={"usage": {"input": 7}}),
        ]
    )
    clock = _DeterministicClock()
    client = PhoenixClient(
        base_url="http://phoenix:6006",
        clock=clock,
        sleeper=clock.sleep,
    )
    enricher = PhoenixTraceEnricher(
        base_url="http://phoenix:6006",
        wait_for_ingestion_seconds=10.0,
        poll_interval_seconds=2.0,
        merge={"metrics.token_input": "$.usage.input"},
        client=client,
    )
    trace = _trace_with_id("abc-123")
    async with enricher:
        out = await enricher.enrich(trace)

    assert out.metrics.token_input == 7
    assert route.call_count == 3
    assert clock.sleeps == [2.0, 2.0]


async def test_deadline_exhausted_raises_failure_soft_error(
    respx_route: respx.MockRouter,
) -> None:
    """Runner's failure-soft hook catches AdapterError; the enricher's job
    is just to raise cleanly when polling gives up."""
    respx_route.get("http://phoenix:6006/v1/traces/missing").mock(
        return_value=httpx.Response(404)
    )
    clock = _DeterministicClock()
    client = PhoenixClient(
        base_url="http://phoenix:6006", clock=clock, sleeper=clock.sleep
    )
    enricher = PhoenixTraceEnricher(
        base_url="http://phoenix:6006",
        wait_for_ingestion_seconds=2.0,
        poll_interval_seconds=1.0,
        merge={"metrics.token_input": "$.usage.input"},
        client=client,
    )
    trace = _trace_with_id("missing")
    async with enricher:
        with pytest.raises(AdapterError, match="did not appear"):
            await enricher.enrich(trace)


# ---- missing trace_id ---------------------------------------------------


async def test_missing_trace_id_raises_adapter_error() -> None:
    enricher = PhoenixTraceEnricher(
        base_url="http://phoenix:6006",
        merge={"metrics.token_input": "$.usage.input"},
    )
    trace = _trace_with_id(None)
    async with enricher:
        with pytest.raises(AdapterError, match="trace_id"):
            await enricher.enrich(trace)


# ---- factory registration -----------------------------------------------


def test_factory_registers_phoenix_enricher() -> None:
    from eval_harness.factories import trace_enricher_factory

    trace_enricher_factory.load_entry_points()
    assert "phoenix" in trace_enricher_factory.registry.names()
