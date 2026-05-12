"""ArizeTraceStore tests.

Reuses the OTel SDK's InMemorySpanExporter to verify the only delta from
`OtelTraceStore` is endpoint + headers + resource_attributes; span
emission is the parent class's job (covered in test_otel_trace_store.py).
Skips when [arize] / [otel] extras aren't installed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("arize.otel")
pytest.importorskip("opentelemetry.sdk.trace.export.in_memory_span_exporter")

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from eval_harness._platforms import arize as arize_mod
from eval_harness._platforms import otel as otel_mod
from eval_harness._platforms.otel import OtelClient
from eval_harness.adapters.trace.arize_trace_store import ArizeTraceStore
from eval_harness.adapters.trace.otel_trace_store import OtelTraceStore
from eval_harness.core.models import Trace, TraceMetrics, TraceOutput

_NOW = datetime(2026, 5, 12, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_registries() -> None:
    otel_mod._clear_registry_for_tests()
    arize_mod._clear_registry_for_tests()


def _make_in_memory_client() -> tuple[OtelClient, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "evalh-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    client = OtelClient(endpoint="https://otlp.arize.com/v1")
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


def test_default_endpoint_is_arize_otlp() -> None:
    store = ArizeTraceStore()
    assert store.endpoint == "https://otlp.arize.com/v1"


def test_space_id_and_api_key_land_in_headers() -> None:
    store = ArizeTraceStore(space_id="s1", api_key="k1")
    assert store.headers["space_id"] == "s1"
    assert store.headers["api_key"] == "k1"


def test_model_id_and_space_id_in_resource_attributes() -> None:
    store = ArizeTraceStore(
        space_id="s1",
        model_id="m1",
        model_version="v3",
        environment="prod",
    )
    assert store.resource_attributes["model_id"] == "m1"
    assert store.resource_attributes["model_version"] == "v3"
    assert store.resource_attributes["arize.space_id"] == "s1"
    assert store.resource_attributes["deployment.environment"] == "prod"


def test_extra_resource_attributes_merge() -> None:
    store = ArizeTraceStore(
        model_id="m1",
        resource_attributes={"deployment.region": "us-west-2"},
    )
    assert store.resource_attributes["deployment.region"] == "us-west-2"
    assert store.resource_attributes["model_id"] == "m1"


# ---- composition with OtelTraceStore (the "thin layer" invariant) ------


def test_arize_store_is_otel_store() -> None:
    """Subclass invariant — Arize doesn't reimplement OTel from scratch."""
    store = ArizeTraceStore()
    assert isinstance(store, OtelTraceStore)


def test_arize_and_otel_with_matching_target_share_tracer_provider() -> None:
    """An ArizeTraceStore and an OtelTraceStore pointed at the same
    endpoint + resource attrs + headers should resolve to the same shared
    `OtelClient` via the platform registry."""
    az = ArizeTraceStore(
        endpoint="https://otlp.arize.com/v1",
        space_id="s1",
        api_key="k1",
        model_id="m1",
    )
    otel = OtelTraceStore(
        endpoint="https://otlp.arize.com/v1",
        headers={"space_id": "s1", "api_key": "k1"},
        resource_attributes={"model_id": "m1", "arize.space_id": "s1"},
    )
    import asyncio

    async def _drive() -> None:
        async with az, otel:
            assert az._client is otel._client

    asyncio.run(_drive())


# ---- span emission goes through OtelTraceStore --------------------------


async def test_save_trace_emits_root_span_with_arize_resource() -> None:
    client, exporter = _make_in_memory_client()
    store = ArizeTraceStore(
        space_id="s1",
        api_key="k1",
        model_id="m1",
        _client=client,
    )
    async with store:
        await store.open("r1", Path("/tmp/r1"))
        await store.save_trace(_trace())

    spans = exporter.get_finished_spans()
    roots = [s for s in spans if s.name == "trace:c1"]
    assert len(roots) == 1
    attrs = dict(roots[0].attributes or {})
    # The OTel parent's attribute shape is preserved; Arize just adjusts
    # endpoint + headers + resource (not span attrs).
    assert attrs["evalh.run_id"] == "r1"
    assert attrs["evalh.case_id"] == "c1"
    assert attrs["evalh.metrics.token_input"] == 10


# ---- factory registration -----------------------------------------------


def test_factory_registers_arize_store() -> None:
    from eval_harness.factories import trace_store_factory

    trace_store_factory.load_entry_points()
    assert "arize" in trace_store_factory.registry.names()
