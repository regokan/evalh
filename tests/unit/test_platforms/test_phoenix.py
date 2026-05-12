"""PhoenixClient platform helper tests.

Uses a respx-mocked httpx.AsyncClient + a deterministic clock so the
ingestion-lag scenarios are reproducible without `time.sleep` or a real
Phoenix server. Skips cleanly when the [phoenix] extra isn't installed.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

pytest.importorskip("phoenix.otel")

from eval_harness._platforms import phoenix as phoenix_mod
from eval_harness._platforms.phoenix import (
    PhoenixClient,
    get_or_create_phoenix_client,
    phoenix_resource_attributes,
    phoenix_to_otel_endpoint,
    release_phoenix_client,
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
def _clean_registry() -> Iterator[None]:
    phoenix_mod._clear_registry_for_tests()
    try:
        yield
    finally:
        phoenix_mod._clear_registry_for_tests()


@pytest.fixture
def respx_route() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


# ---- pure helpers --------------------------------------------------------


def test_phoenix_to_otel_endpoint_appends_path() -> None:
    assert phoenix_to_otel_endpoint("http://phoenix:6006") == "http://phoenix:6006/v1/traces"


def test_phoenix_to_otel_endpoint_strips_trailing_slash() -> None:
    assert phoenix_to_otel_endpoint("http://phoenix:6006/") == "http://phoenix:6006/v1/traces"


def test_phoenix_resource_attributes_includes_project_name() -> None:
    attrs = phoenix_resource_attributes(project_name="evalh-demo")
    assert attrs == {"openinference.project.name": "evalh-demo"}


def test_phoenix_resource_attributes_merges_extra() -> None:
    attrs = phoenix_resource_attributes(
        project_name="evalh-demo", extra={"deployment.environment": "staging"}
    )
    assert attrs["openinference.project.name"] == "evalh-demo"
    assert attrs["deployment.environment"] == "staging"


def test_phoenix_resource_attributes_no_project_returns_extras_only() -> None:
    assert phoenix_resource_attributes(project_name=None, extra={"k": "v"}) == {"k": "v"}


# ---- construction --------------------------------------------------------


def test_base_url_required() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        PhoenixClient(base_url="")


def test_api_key_auto_populates_authorization_header() -> None:
    client = PhoenixClient(
        base_url="http://phoenix:6006",
        api_key="abc",
        _http_client=httpx.AsyncClient(),
    )
    assert client.headers.get("Authorization") == "Bearer abc"


def test_explicit_authorization_header_wins() -> None:
    client = PhoenixClient(
        base_url="http://phoenix:6006",
        api_key="abc",
        headers={"Authorization": "Custom xyz"},
        _http_client=httpx.AsyncClient(),
    )
    # Caller-provided header isn't overwritten.
    assert client.headers["Authorization"] == "Custom xyz"


def test_resource_attributes_carry_project_name() -> None:
    client = PhoenixClient(
        base_url="http://phoenix:6006",
        project_name="evalh-demo",
        _http_client=httpx.AsyncClient(),
    )
    assert client.resource_attributes["openinference.project.name"] == "evalh-demo"


# ---- OtelClient sharing (the headline composition invariant) ------------


def test_two_phoenix_clients_same_target_share_otel_client() -> None:
    """The bead's key invariant: Phoenix composes with OTel, so two
    PhoenixClients with the same (base_url, api_key, project, headers)
    share one underlying `OtelClient` (and therefore one TracerProvider)."""
    a = get_or_create_phoenix_client(
        base_url="http://phoenix:6006", project_name="evalh"
    )
    b = get_or_create_phoenix_client(
        base_url="http://phoenix:6006", project_name="evalh"
    )
    assert a is b
    assert a.otel_client is b.otel_client
    assert a.otel_client.get_tracer_provider() is b.otel_client.get_tracer_provider()
    release_phoenix_client(a)
    release_phoenix_client(b)


def test_phoenix_and_otel_clients_with_same_endpoint_share_tracer_provider() -> None:
    """Cross-family sharing: a PhoenixClient and a direct OtelClient both
    targeting `http://phoenix:6006/v1/traces` with matching attrs share the
    same underlying TracerProvider — verifies "no duplicate OTel export
    logic" at the registry level."""
    from eval_harness._platforms.otel import get_or_create_otel_client, release_otel_client

    base = "http://phoenix:6006"
    otel = get_or_create_otel_client(
        endpoint=phoenix_to_otel_endpoint(base),
        resource_attributes=phoenix_resource_attributes(project_name="evalh"),
    )
    phx = get_or_create_phoenix_client(base_url=base, project_name="evalh")
    assert phx.otel_client is otel
    release_phoenix_client(phx)
    release_otel_client(otel)


def test_different_project_names_get_distinct_clients() -> None:
    a = get_or_create_phoenix_client(
        base_url="http://phoenix:6006", project_name="alpha"
    )
    b = get_or_create_phoenix_client(
        base_url="http://phoenix:6006", project_name="bravo"
    )
    assert a is not b


# ---- refcount lifecycle --------------------------------------------------


def test_refcount_drives_shutdown() -> None:
    a = get_or_create_phoenix_client(base_url="http://phoenix:6006")
    b = get_or_create_phoenix_client(base_url="http://phoenix:6006")
    assert a is b
    assert sum(phoenix_mod._registry_snapshot().values()) == 2

    release_phoenix_client(a)
    assert sum(phoenix_mod._registry_snapshot().values()) == 1

    release_phoenix_client(b)
    assert phoenix_mod._registry_snapshot() == {}


# ---- fetch_trace ---------------------------------------------------------


async def test_fetch_trace_returns_payload_on_first_attempt(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://phoenix:6006/v1/traces/abc").mock(
        return_value=httpx.Response(200, json={"id": "abc", "messages": ["hi"]})
    )
    clock = _DeterministicClock()
    client = PhoenixClient(
        base_url="http://phoenix:6006",
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
    route = respx_route.get("http://phoenix:6006/v1/traces/abc").mock(
        side_effect=[
            httpx.Response(404),
            httpx.Response(404),
            httpx.Response(200, json={"id": "abc"}),
        ]
    )
    clock = _DeterministicClock()
    client = PhoenixClient(
        base_url="http://phoenix:6006",
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
    respx_route.get("http://phoenix:6006/v1/traces/missing").mock(
        return_value=httpx.Response(404)
    )
    clock = _DeterministicClock()
    client = PhoenixClient(
        base_url="http://phoenix:6006",
        clock=clock,
        sleeper=clock.sleep,
    )
    # 2s budget, 1s poll -> at most 2 polls before the deadline elapses.
    payload = await client.fetch_trace(
        "missing", wait_for_ingestion_seconds=2.0, poll_interval_seconds=1.0
    )
    assert payload is None
    await client.aclose()


async def test_fetch_trace_unwraps_data_envelope(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://phoenix:6006/v1/traces/abc").mock(
        return_value=httpx.Response(200, json={"data": {"id": "abc", "messages": []}})
    )
    client = PhoenixClient(base_url="http://phoenix:6006")
    payload = await client.fetch_trace("abc")
    assert payload == {"id": "abc", "messages": []}
    await client.aclose()


# ---- search_traces -------------------------------------------------------


async def test_search_traces_forwards_project_name(
    respx_route: respx.MockRouter,
) -> None:
    route = respx_route.get("http://phoenix:6006/v1/spans").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "t1"}, {"id": "t2"}]}
        )
    )
    client = PhoenixClient(
        base_url="http://phoenix:6006",
        project_name="evalh-demo",
    )
    out = await client.search_traces({"start_time": "2026-05-01"})
    assert out == [{"id": "t1"}, {"id": "t2"}]
    # project_name was forwarded as a query param.
    sent = route.calls[0].request.url
    assert "project_name=evalh-demo" in str(sent)
    assert "start_time=2026-05-01" in str(sent)
    await client.aclose()


async def test_search_traces_handles_top_level_list(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://phoenix:6006/v1/spans").mock(
        return_value=httpx.Response(200, json=[{"id": "t1"}])
    )
    client = PhoenixClient(base_url="http://phoenix:6006")
    assert await client.search_traces({}) == [{"id": "t1"}]
    await client.aclose()


# ---- shutdown ------------------------------------------------------------


async def test_shutdown_idempotent() -> None:
    client = PhoenixClient(base_url="http://phoenix:6006")
    client.shutdown()
    client.shutdown()  # no-op
    await client.aclose()


async def test_fetch_trace_after_shutdown_raises() -> None:
    client = PhoenixClient(base_url="http://phoenix:6006")
    client.shutdown()
    with pytest.raises(RuntimeError, match="after shutdown"):
        await client.fetch_trace("abc")
