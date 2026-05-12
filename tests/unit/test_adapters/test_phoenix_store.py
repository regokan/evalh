"""PhoenixTraceStore tests.

Reuses the OTel SDK's InMemorySpanExporter to verify the only delta from
OtelTraceStore is endpoint + resource_attributes; the span emission is
the parent class's job (already covered in test_otel_trace_store.py).
Skips when [phoenix] / [otel] extras aren't installed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("phoenix.otel")
pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from eval_harness._platforms import otel as otel_mod
from eval_harness._platforms import phoenix as phoenix_mod
from eval_harness._platforms.otel import OtelClient
from eval_harness.adapters.trace.otel_trace_store import OtelTraceStore
from eval_harness.adapters.trace.phoenix_trace_store import PhoenixTraceStore
from eval_harness.core.errors import ConfigError
from eval_harness.core.models import Trace, TraceMetrics, TraceOutput

_NOW = datetime(2026, 5, 12, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_registries() -> None:
    otel_mod._clear_registry_for_tests()
    phoenix_mod._clear_registry_for_tests()


def _make_in_memory_client() -> tuple[OtelClient, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "evalh-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    client = OtelClient(endpoint="http://phoenix:6006/v1/traces")
    client._provider = provider
    return client, exporter


def _trace(**overrides: Any) -> Trace:
    defaults: dict[str, Any] = dict(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=42,
        input={"q": "ping"},
        output=TraceOutput(final_answer="pong"),
        metrics=TraceMetrics(token_input=10, token_output=5, cost_usd=0.001),
    )
    defaults.update(overrides)
    return Trace(**defaults)


# ---- config / construction ----------------------------------------------


def test_base_url_required() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        PhoenixTraceStore(base_url="")


def test_endpoint_derived_from_base_url() -> None:
    store = PhoenixTraceStore(base_url="http://phoenix:6006")
    assert store.endpoint == "http://phoenix:6006/v1/traces"


def test_api_key_auto_authorization_header() -> None:
    store = PhoenixTraceStore(base_url="http://phoenix:6006", api_key="abc")
    assert store.headers["Authorization"] == "Bearer abc"


def test_resource_attributes_carry_project_name() -> None:
    store = PhoenixTraceStore(
        base_url="http://phoenix:6006", project_name="evalh-demo"
    )
    assert store.resource_attributes == {"openinference.project.name": "evalh-demo"}


def test_resource_attributes_merge_extras() -> None:
    store = PhoenixTraceStore(
        base_url="http://phoenix:6006",
        project_name="evalh-demo",
        resource_attributes={"deployment.environment": "staging"},
    )
    assert store.resource_attributes["openinference.project.name"] == "evalh-demo"
    assert store.resource_attributes["deployment.environment"] == "staging"


# ---- composition with OtelTraceStore (the "thin layer" invariant) ------


def test_phoenix_store_is_otel_store() -> None:
    """Subclass invariant — Phoenix doesn't reimplement OTel from scratch."""
    store = PhoenixTraceStore(base_url="http://phoenix:6006")
    assert isinstance(store, OtelTraceStore)


def test_phoenix_and_otel_with_matching_target_share_tracer_provider() -> None:
    """A PhoenixTraceStore and an OtelTraceStore pointed at the same
    derived endpoint + resource attrs should resolve to the same shared
    `OtelClient` via the platform registry."""
    base = "http://phoenix:6006"
    phx = PhoenixTraceStore(base_url=base, project_name="evalh")
    otel = OtelTraceStore(
        endpoint="http://phoenix:6006/v1/traces",
        resource_attributes={"openinference.project.name": "evalh"},
    )
    import asyncio

    async def _drive() -> None:
        async with phx, otel:
            assert phx._client is otel._client

    asyncio.run(_drive())


# ---- span emission goes through OtelTraceStore --------------------------


async def test_save_trace_emits_root_span_with_phoenix_resource() -> None:
    client, exporter = _make_in_memory_client()
    store = PhoenixTraceStore(
        base_url="http://phoenix:6006",
        project_name="evalh-demo",
        _client=client,
    )
    async with store:
        await store.open("r1", Path("/tmp/r1"))
        await store.save_trace(_trace())

    spans = exporter.get_finished_spans()
    roots = [s for s in spans if s.name == "trace:c1"]
    assert len(roots) == 1
    attrs = dict(roots[0].attributes or {})
    # The OTel parent's attribute shape is preserved; Phoenix just adjusts
    # endpoint + resource (not span attrs).
    assert attrs["evalh.run_id"] == "r1"
    assert attrs["evalh.case_id"] == "c1"
    assert attrs["evalh.metrics.token_input"] == 10


# ---- factory registration -----------------------------------------------


def test_factory_registers_phoenix() -> None:
    from eval_harness.factories import trace_store_factory

    trace_store_factory.load_entry_points()
    assert "phoenix" in trace_store_factory.registry.names()
