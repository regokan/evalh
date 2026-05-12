"""ArizeClient platform helper tests.

Uses a respx-mocked httpx.AsyncClient + a deterministic clock so the
ingestion-lag scenarios are reproducible without `time.sleep` or a real
Arize server. Skips cleanly when the [arize] extra isn't installed.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

pytest.importorskip("arize.otel")

from eval_harness._platforms import arize as arize_mod
from eval_harness._platforms.arize import (
    ArizeClient,
    arize_otel_headers,
    arize_resource_attributes,
    get_or_create_arize_client,
    release_arize_client,
)
from eval_harness.core.errors import ConfigError


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
def _clean_registries() -> Iterator[None]:
    from eval_harness._platforms import otel as otel_mod

    otel_mod._clear_registry_for_tests()
    arize_mod._clear_registry_for_tests()
    try:
        yield
    finally:
        arize_mod._clear_registry_for_tests()
        otel_mod._clear_registry_for_tests()


@pytest.fixture
def respx_route() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


# ---- pure helpers --------------------------------------------------------


def test_resource_attributes_carries_model_id_and_space_id() -> None:
    attrs = arize_resource_attributes(
        model_id="m1", space_id="s1", environment="prod"
    )
    assert attrs["model_id"] == "m1"
    assert attrs["arize.space_id"] == "s1"
    assert attrs["deployment.environment"] == "prod"


def test_resource_attributes_extra_merges() -> None:
    attrs = arize_resource_attributes(
        model_id="m1", extra={"deployment.version": "v3"}
    )
    assert attrs["deployment.version"] == "v3"


def test_otel_headers_includes_space_id_and_api_key() -> None:
    h = arize_otel_headers(space_id="s1", api_key="k")
    assert h == {"space_id": "s1", "api_key": "k"}


def test_otel_headers_extra_does_not_overwrite_unless_explicit() -> None:
    h = arize_otel_headers(space_id="s1", api_key="k", extra={"x-trace": "1"})
    assert h["space_id"] == "s1"
    assert h["x-trace"] == "1"


# ---- construction --------------------------------------------------------


def test_endpoint_required() -> None:
    with pytest.raises(ConfigError, match="endpoint"):
        ArizeClient(endpoint="")


def test_construction_populates_default_endpoint() -> None:
    client = ArizeClient(_http_client=httpx.AsyncClient())
    assert client.endpoint == "https://otlp.arize.com/v1"


def test_resource_attributes_present_on_client() -> None:
    client = ArizeClient(
        endpoint="http://otlp.local",
        model_id="m1",
        space_id="s1",
        _http_client=httpx.AsyncClient(),
    )
    assert client.resource_attributes["model_id"] == "m1"
    assert client.resource_attributes["arize.space_id"] == "s1"


# ---- OtelClient sharing (the headline composition invariant) ------------


def test_two_arize_clients_same_target_share_otel_client() -> None:
    """Two ArizeClients with the same config share one underlying
    `OtelClient` (and therefore one TracerProvider)."""
    a = get_or_create_arize_client(
        endpoint="http://otlp.local", model_id="m1", space_id="s1"
    )
    b = get_or_create_arize_client(
        endpoint="http://otlp.local", model_id="m1", space_id="s1"
    )
    assert a is b
    assert a.otel_client is b.otel_client
    assert a.otel_client.get_tracer_provider() is b.otel_client.get_tracer_provider()
    release_arize_client(a)
    release_arize_client(b)


def test_arize_and_otel_clients_with_same_endpoint_share_tracer_provider() -> None:
    """Cross-family sharing: an ArizeClient and a direct OtelClient pointed
    at the same endpoint with matching headers + resource attrs share the
    same underlying TracerProvider — proves the 'no duplicate OTel export
    logic' invariant at the registry level."""
    from eval_harness._platforms.otel import (
        get_or_create_otel_client,
        release_otel_client,
    )

    az = get_or_create_arize_client(
        endpoint="http://otlp.local", model_id="m1", space_id="s1", api_key="k"
    )
    otel = get_or_create_otel_client(
        endpoint="http://otlp.local",
        headers=arize_otel_headers(space_id="s1", api_key="k"),
        resource_attributes=arize_resource_attributes(model_id="m1", space_id="s1"),
    )
    assert az.otel_client is otel
    release_arize_client(az)
    release_otel_client(otel)


def test_different_model_ids_get_distinct_clients() -> None:
    a = get_or_create_arize_client(endpoint="http://otlp.local", model_id="alpha")
    b = get_or_create_arize_client(endpoint="http://otlp.local", model_id="bravo")
    assert a is not b


# ---- refcount lifecycle --------------------------------------------------


def test_refcount_drives_shutdown() -> None:
    a = get_or_create_arize_client(endpoint="http://otlp.local")
    b = get_or_create_arize_client(endpoint="http://otlp.local")
    assert a is b
    assert sum(arize_mod._registry_snapshot().values()) == 2

    release_arize_client(a)
    assert sum(arize_mod._registry_snapshot().values()) == 1

    release_arize_client(b)
    assert arize_mod._registry_snapshot() == {}


# ---- fetch_trace ---------------------------------------------------------


async def test_fetch_trace_returns_payload_on_first_attempt(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://otlp.local/v1/traces/abc").mock(
        return_value=httpx.Response(200, json={"id": "abc", "messages": ["hi"]})
    )
    clock = _DeterministicClock()
    client = ArizeClient(
        endpoint="http://otlp.local/v1",
        clock=clock,
        sleeper=clock.sleep,
    )
    payload = await client.fetch_trace("abc", wait_for_ingestion_seconds=0.0)
    assert payload == {"id": "abc", "messages": ["hi"]}
    assert clock.sleeps == []
    await client.aclose()


async def test_fetch_trace_polls_with_deterministic_clock(
    respx_route: respx.MockRouter,
) -> None:
    """404 -> wait -> 404 -> wait -> 200. Three GETs, two waits, no wall time."""
    route = respx_route.get("http://otlp.local/v1/traces/abc").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(404),
            httpx.Response(200, json={"id": "abc"}),
        ]
    )
    clock = _DeterministicClock()
    client = ArizeClient(
        endpoint="http://otlp.local/v1",
        clock=clock,
        sleeper=clock.sleep,
    )
    payload = await client.fetch_trace(
        "abc", wait_for_ingestion_seconds=10.0, poll_interval_seconds=2.0
    )
    assert payload == {"id": "abc"}
    assert route.call_count == 3
    assert clock.sleeps == [2.0, 2.0]
    await client.aclose()


async def test_fetch_trace_returns_none_when_deadline_exceeded(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://otlp.local/v1/traces/missing").mock(
        return_value=httpx.Response(404)
    )
    clock = _DeterministicClock()
    client = ArizeClient(
        endpoint="http://otlp.local/v1",
        clock=clock,
        sleeper=clock.sleep,
    )
    payload = await client.fetch_trace(
        "missing", wait_for_ingestion_seconds=2.0, poll_interval_seconds=1.0
    )
    assert payload is None
    await client.aclose()


async def test_fetch_trace_unwraps_data_envelope(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://otlp.local/v1/traces/abc").mock(
        return_value=httpx.Response(200, json={"data": {"id": "abc", "messages": []}})
    )
    client = ArizeClient(endpoint="http://otlp.local/v1")
    payload = await client.fetch_trace("abc")
    assert payload == {"id": "abc", "messages": []}
    await client.aclose()


# ---- search_traces -------------------------------------------------------


async def test_search_traces_forwards_model_and_space_ids(
    respx_route: respx.MockRouter,
) -> None:
    route = respx_route.get("http://otlp.local/v1/spans").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "t1"}, {"id": "t2"}]}
        )
    )
    client = ArizeClient(
        endpoint="http://otlp.local/v1",
        model_id="m1",
        space_id="s1",
    )
    out = await client.search_traces({"start_time": "2026-05-01"})
    assert out == [{"id": "t1"}, {"id": "t2"}]
    sent = str(route.calls[0].request.url)
    assert "model_id=m1" in sent
    assert "space_id=s1" in sent
    assert "start_time=2026-05-01" in sent
    await client.aclose()


async def test_search_traces_handles_top_level_list(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://otlp.local/v1/spans").mock(
        return_value=httpx.Response(200, json=[{"id": "t1"}])
    )
    client = ArizeClient(endpoint="http://otlp.local/v1")
    assert await client.search_traces({}) == [{"id": "t1"}]
    await client.aclose()


# ---- shutdown ------------------------------------------------------------


async def test_shutdown_idempotent() -> None:
    client = ArizeClient(endpoint="http://otlp.local/v1")
    client.shutdown()
    client.shutdown()  # no-op
    await client.aclose()


async def test_fetch_trace_after_shutdown_raises() -> None:
    client = ArizeClient(endpoint="http://otlp.local/v1")
    client.shutdown()
    with pytest.raises(RuntimeError, match="after shutdown"):
        await client.fetch_trace("abc")
