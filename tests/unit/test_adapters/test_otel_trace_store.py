"""OtelTraceStore tests — uses the OTel SDK's `InMemorySpanExporter` so we
verify span shape without a real OTLP collector.

Skips cleanly when the [otel] extra isn't installed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk.trace")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from eval_harness._platforms import otel as otel_mod
from eval_harness._platforms.otel import OtelClient
from eval_harness.adapters.trace.otel_trace_store import OtelTraceStore
from eval_harness.core.models import (
    EvaluationResult,
    FileDiff,
    FileManifest,
    FilesystemArtifact,
    RunSummary,
    ToolCall,
    ToolResult,
    Trace,
    TraceMetrics,
    TraceOutput,
    VariantSummary,
)

_NOW = datetime(2026, 5, 12, 14, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    otel_mod._clear_registry_for_tests()


def _make_client_with_in_memory_exporter() -> tuple[OtelClient, InMemorySpanExporter]:
    """Build an OtelClient whose TracerProvider exports to an in-memory list
    of spans. The store treats this exactly like a real OTLP-backed client."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "evalh-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    client = OtelClient(endpoint="http://test:4318/v1/traces")
    # Bypass the lazy OTLP construction.
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


def _result(**overrides: Any) -> EvaluationResult:
    defaults: dict[str, Any] = dict(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        evaluator="contains",
        evaluator_type="contains_text",
        passed=True,
        score=1.0,
        reason="ok",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
    )
    defaults.update(overrides)
    return EvaluationResult(**defaults)


def _by_name(spans: list[Any], name: str) -> list[Any]:
    return [s for s in spans if s.name == name]


# ---- factory registration -----------------------------------------------


def test_factory_registers_otel() -> None:
    from eval_harness.factories import trace_store_factory

    trace_store_factory.load_entry_points()
    assert "otel" in trace_store_factory.registry.names()


# ---- lifecycle / sharing -------------------------------------------------


async def test_two_otel_adapters_same_endpoint_share_tracer_provider() -> None:
    """ev-joq's shared-singleton invariant must survive into v1-supplement:
    two OtelTraceStores with the same target acquire the same OtelClient
    (and therefore the same TracerProvider) via __aenter__."""
    store_a = OtelTraceStore(endpoint="http://collector:4318/v1/traces")
    store_b = OtelTraceStore(endpoint="http://collector:4318/v1/traces")
    async with store_a, store_b:
        assert store_a._client is store_b._client
        assert (
            store_a._client.get_tracer_provider()
            is store_b._client.get_tracer_provider()
        )


# ---- save_trace ---------------------------------------------------------


async def test_save_trace_emits_root_span_with_attributes() -> None:
    client, exporter = _make_client_with_in_memory_exporter()
    store = OtelTraceStore(
        endpoint="http://test:4318/v1/traces", _client=client
    )
    async with store:
        await store.open("r1", Path("/tmp/r1"))
        await store.save_trace(_trace())

    spans = exporter.get_finished_spans()
    roots = _by_name(spans, "trace:c1")
    assert len(roots) == 1
    attrs = dict(roots[0].attributes or {})
    assert attrs["evalh.run_id"] == "r1"
    assert attrs["evalh.case_id"] == "c1"
    assert attrs["evalh.variant_name"] == "v1"
    assert attrs["evalh.latency_ms"] == 42
    assert attrs["evalh.metrics.token_input"] == 10
    assert attrs["evalh.metrics.cost_usd"] == pytest.approx(0.001)
    assert attrs["evalh.output.final_answer"] == "pong"


async def test_save_trace_emits_tool_call_child_spans() -> None:
    client, exporter = _make_client_with_in_memory_exporter()
    store = OtelTraceStore(
        endpoint="http://test:4318/v1/traces", _client=client
    )
    trace = _trace(
        tool_calls=[
            ToolCall(id="t1", name="lookup", arguments={"q": "ABC123"}),
            ToolCall(id="t2", name="rank", arguments={"items": [1, 2]}),
        ],
        tool_results=[
            ToolResult(tool_call_id="t1", name="lookup", content={"hit": True}),
        ],
    )
    async with store:
        await store.save_trace(trace)

    spans = exporter.get_finished_spans()
    children = [s for s in spans if s.name.startswith("tool:")]
    assert {s.name for s in children} == {"tool:lookup", "tool:rank"}
    lookup = _by_name(children, "tool:lookup")[0]
    assert "lookup" in dict(lookup.attributes or {})["evalh.tool.name"]
    # The matching tool_result fires as an event on the lookup span.
    assert any(e.name == "tool_result" for e in lookup.events)


async def test_save_trace_records_error_attributes() -> None:
    from eval_harness.core.models import TraceError

    client, exporter = _make_client_with_in_memory_exporter()
    store = OtelTraceStore(
        endpoint="http://test:4318/v1/traces", _client=client
    )
    trace = _trace(error=TraceError(type="timeout", message="timed out after 60s"))
    async with store:
        await store.save_trace(trace)

    root = _by_name(exporter.get_finished_spans(), "trace:c1")[0]
    attrs = dict(root.attributes or {})
    assert attrs["evalh.error.type"] == "timeout"
    assert "60s" in attrs["evalh.error.message"]


# ---- save_evaluation ----------------------------------------------------


async def test_save_evaluation_emits_one_span_per_result() -> None:
    client, exporter = _make_client_with_in_memory_exporter()
    store = OtelTraceStore(
        endpoint="http://test:4318/v1/traces", _client=client
    )
    async with store:
        await store.save_evaluation(
            "c1",
            "v1",
            [
                _result(evaluator="contains", passed=True),
                _result(evaluator="judge", passed=False, reason="off-topic"),
            ],
        )

    spans = exporter.get_finished_spans()
    names = {s.name for s in spans if s.name.startswith("evaluation:")}
    assert names == {"evaluation:contains", "evaluation:judge"}
    judge = next(s for s in spans if s.name == "evaluation:judge")
    attrs = dict(judge.attributes or {})
    assert attrs["evalh.evaluator.passed"] is False
    assert attrs["evalh.evaluator.reason"] == "off-topic"


async def test_save_evaluation_no_results_is_noop() -> None:
    client, exporter = _make_client_with_in_memory_exporter()
    store = OtelTraceStore(
        endpoint="http://test:4318/v1/traces", _client=client
    )
    async with store:
        await store.save_evaluation("c1", "v1", [])
    assert exporter.get_finished_spans() == ()


# ---- save_artifact ------------------------------------------------------


async def test_save_artifact_emits_diff_summary_span() -> None:
    client, exporter = _make_client_with_in_memory_exporter()
    store = OtelTraceStore(
        endpoint="http://test:4318/v1/traces", _client=client
    )
    artifact = FilesystemArtifact(
        case_id="c1",
        variant_name="v1",
        workspace_kind="tempdir_snapshot",
        before_manifest=FileManifest(files={}),
        after_manifest=FileManifest(files={}),
        diff=FileDiff(
            added=["new.py"],
            removed=[],
            modified=["src/app.py", "src/lib.py"],
        ),
        artifacts_path="/tmp/x",
    )
    async with store:
        await store.open("r1", Path("/tmp/r1"))
        await store.save_artifact(artifact)

    span = _by_name(exporter.get_finished_spans(), "artifact:c1")[0]
    attrs = dict(span.attributes or {})
    assert attrs["evalh.artifact.added"] == 1
    assert attrs["evalh.artifact.modified"] == 2


# ---- save_summary -------------------------------------------------------


async def test_save_summary_emits_run_level_span_and_variant_events() -> None:
    client, exporter = _make_client_with_in_memory_exporter()
    store = OtelTraceStore(
        endpoint="http://test:4318/v1/traces", _client=client
    )
    summary = RunSummary(
        run_id="r1",
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="abc",
        cases_total=2,
        variants=[
            VariantSummary(
                name="v1",
                cases_total=2,
                cases_passed=2,
                cases_errored=0,
                pass_rate=1.0,
                avg_latency_ms=42.0,
                avg_cost_usd=None,
                avg_tokens_input=None,
                avg_tokens_output=None,
            ),
            VariantSummary(
                name="v2",
                cases_total=2,
                cases_passed=1,
                cases_errored=0,
                pass_rate=0.5,
                avg_latency_ms=50.0,
                avg_cost_usd=None,
                avg_tokens_input=None,
                avg_tokens_output=None,
            ),
        ],
        by_evaluator=[],
    )
    async with store:
        await store.save_summary(summary)

    span = _by_name(exporter.get_finished_spans(), "run_summary:r1")[0]
    attrs = dict(span.attributes or {})
    assert attrs["evalh.cases_total"] == 2
    assert attrs["evalh.variants.count"] == 2
    event_names = {e.name for e in span.events}
    assert event_names == {"variant:v1", "variant:v2"}


# ---- reads (write-only contract) ----------------------------------------


async def test_otel_store_iter_traces_is_empty() -> None:
    store = OtelTraceStore(endpoint="http://test:4318/v1/traces")
    async with store:
        items = [t async for t in store.iter_traces()]
    assert items == []


async def test_otel_store_iter_results_is_empty() -> None:
    store = OtelTraceStore(endpoint="http://test:4318/v1/traces")
    async with store:
        items = [r async for r in store.iter_results()]
    assert items == []


async def test_otel_store_load_summary_returns_none() -> None:
    store = OtelTraceStore(endpoint="http://test:4318/v1/traces")
    async with store:
        assert await store.load_summary("r1") is None


async def test_otel_store_list_run_ids_is_empty() -> None:
    store = OtelTraceStore(endpoint="http://test:4318/v1/traces")
    async with store:
        assert await store.list_run_ids() == []
