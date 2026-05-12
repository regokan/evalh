"""OTel TraceStore — pushes our Traces as OpenTelemetry spans.

Shape:
  - Root span per (case_id, variant_name) tagged with run-level + trace-level
    attributes (token counts, cost, latency, error type/message if present).
  - One child span per `Trace.tool_calls[*]` carrying the tool name +
    arguments JSON. Tool results land as span events on the matching tool
    span (matched by `tool_call_id`).
  - One *sibling* span per evaluator result (one per (case, variant,
    evaluator) tuple) tagged with passed / score / reason — cleaner for
    UI filtering than packing N events onto the root.
  - One run-summary span at finalize time with cases_total / pass_rate /
    error counts so per-run rollups are visible in the backend.

Reads are not supported — this is a write-only secondary sink. Put a
queryable backend (local_files, sqlite, postgres) first in `output[]` if
you need `evalh inspect` / `compare` / `re-evaluate` against the run.

See docs/Observability.md > "OTel TraceStore".
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from eval_harness._platforms.otel import (
    OtelClient,
    get_or_create_otel_client,
    release_otel_client,
)
from eval_harness.core.models import (
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    Trace,
)

if TYPE_CHECKING:
    pass

_TRACER_NAME = "eval_harness.otel_trace_store"


class OtelTraceStore:
    """TraceStore that pushes spans to any OTLP-compatible backend."""

    def __init__(
        self,
        *,
        endpoint: str,
        headers: dict[str, str] | None = None,
        protocol: str = "http",
        resource_attributes: dict[str, str] | None = None,
        # Test seam: callers may pre-construct an OtelClient (e.g. backed by
        # an InMemorySpanExporter) and inject it here. Production callers
        # leave this unset; the store acquires a shared client per
        # `get_or_create_otel_client` semantics.
        _client: OtelClient | None = None,
        **kwargs: Any,
    ) -> None:
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.protocol = protocol
        self.resource_attributes = dict(resource_attributes or {})
        self._owns_client = _client is None
        self._client: OtelClient | None = _client
        self._run_id: str | None = None
        self._run_dir: Path | None = None
        # Stash extra kwargs (e.g. `type`) so the factory's loose dispatch
        # doesn't break us — matches the local_files store's tolerance.
        self._extra = kwargs

    # ---- lifecycle ------------------------------------------------------

    async def __aenter__(self) -> Self:
        if self._client is None:
            self._client = get_or_create_otel_client(
                endpoint=self.endpoint,
                headers=self.headers,
                protocol=self.protocol,
                resource_attributes=self.resource_attributes,
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client and self._client is not None:
            release_otel_client(self._client)
            self._client = None

    async def open(self, run_id: str, run_dir: Path) -> None:
        self._run_id = run_id
        self._run_dir = run_dir

    async def close(self) -> None:
        # Shutdown is owned by `release_otel_client` via __aexit__.
        return None

    # ---- writes ---------------------------------------------------------

    async def save_trace(self, trace: Trace) -> None:
        tracer = self._tracer()
        start_ns = _to_ns(trace.started_at)
        end_ns = _to_ns(trace.finished_at)
        root = tracer.start_span(
            name=f"trace:{trace.case_id}",
            start_time=start_ns,
            attributes=_trace_attributes(trace, self._run_id),
        )
        try:
            if trace.error is not None:
                root.set_attribute("evalh.error.type", trace.error.type)
                root.set_attribute("evalh.error.message", trace.error.message)
            # Tool calls -> child spans. Tool results join their parent by
            # tool_call_id (when present) as a span event; otherwise they
            # land on the root span as a sibling event so nothing is lost.
            results_by_call_id = {
                r.tool_call_id: r for r in trace.tool_results if r.tool_call_id
            }
            for call in trace.tool_calls:
                child_start = _to_ns(call.started_at) or start_ns
                child = tracer.start_span(
                    name=f"tool:{call.name}",
                    start_time=child_start,
                    attributes={
                        "evalh.tool.name": call.name,
                        "evalh.tool.id": call.id or "",
                        "evalh.tool.arguments": json.dumps(call.arguments, default=str),
                    },
                )
                if call.id and call.id in results_by_call_id:
                    res = results_by_call_id[call.id]
                    child.add_event(
                        "tool_result",
                        attributes={
                            "evalh.tool.result": _stringify(res.content),
                        },
                    )
                child.end(end_time=end_ns)
            # Tool results without a matching call — surface them so they're
            # not silently dropped.
            orphan_results = [r for r in trace.tool_results if not r.tool_call_id]
            for res in orphan_results:
                root.add_event(
                    "tool_result_orphan",
                    attributes={
                        "evalh.tool.name": res.name,
                        "evalh.tool.result": _stringify(res.content),
                    },
                )
        finally:
            root.end(end_time=end_ns)

    async def save_evaluation(
        self,
        case_id: str,
        variant: str,
        results: list[EvaluationResult],
    ) -> None:
        if not results:
            return
        tracer = self._tracer()
        for r in results:
            start_ns = _to_ns(r.started_at)
            end_ns = _to_ns(r.finished_at)
            span = tracer.start_span(
                name=f"evaluation:{r.evaluator}",
                start_time=start_ns,
                attributes={
                    "evalh.run_id": r.run_id,
                    "evalh.case_id": r.case_id,
                    "evalh.variant_name": r.variant_name,
                    "evalh.evaluator.name": r.evaluator,
                    "evalh.evaluator.type": r.evaluator_type,
                    "evalh.evaluator.passed": bool(r.passed),
                    "evalh.evaluator.score": (
                        float(r.score) if r.score is not None else float("nan")
                    ),
                    "evalh.evaluator.reason": r.reason,
                    "evalh.evaluator.latency_ms": int(r.latency_ms),
                },
            )
            if r.error is not None:
                span.set_attribute("evalh.error.type", r.error.type)
                span.set_attribute("evalh.error.message", r.error.message)
            span.end(end_time=end_ns)

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        # Surface the diff shape so it's visible in the trace UI; the actual
        # bytes stay on the canonical sink. Implemented as a short-lived
        # span rather than an event so it groups under the case_id parent
        # filter in most backends.
        tracer = self._tracer()
        span = tracer.start_span(
            name=f"artifact:{artifact.case_id}",
            attributes={
                "evalh.run_id": self._run_id or "",
                "evalh.case_id": artifact.case_id,
                "evalh.variant_name": artifact.variant_name,
                "evalh.artifact.workspace_kind": artifact.workspace_kind,
                "evalh.artifact.added": len(artifact.diff.added),
                "evalh.artifact.removed": len(artifact.diff.removed),
                "evalh.artifact.modified": len(artifact.diff.modified),
            },
        )
        span.end()

    async def save_summary(self, summary: RunSummary) -> None:
        tracer = self._tracer()
        span = tracer.start_span(
            name=f"run_summary:{summary.run_id}",
            attributes={
                "evalh.run_id": summary.run_id,
                "evalh.cases_total": int(summary.cases_total),
                "evalh.config_hash": summary.config_hash,
                "evalh.variants.count": len(summary.variants),
            },
        )
        for variant in summary.variants:
            span.add_event(
                f"variant:{variant.name}",
                attributes={
                    "evalh.variant.name": variant.name,
                    "evalh.variant.cases_passed": variant.cases_passed,
                    "evalh.variant.cases_errored": variant.cases_errored,
                    "evalh.variant.pass_rate": float(variant.pass_rate),
                    "evalh.variant.avg_latency_ms": float(variant.avg_latency_ms),
                },
            )
        span.end()

    # ---- reads (intentionally write-only) -------------------------------

    def iter_traces(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[Trace]:
        return _empty_iter()

    def iter_results(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[EvaluationResult]:
        return _empty_iter()

    async def load_summary(self, run_id: str) -> RunSummary | None:
        # OTel is write-only; consumers should target the canonical sink
        # (local_files / sqlite / postgres) for reads.
        return None

    async def list_run_ids(self) -> list[str]:
        return []

    # ---- helpers --------------------------------------------------------

    def _tracer(self) -> Any:
        client = self._client
        if client is None:
            raise RuntimeError(
                "OtelTraceStore.save_* called outside the `async with` context"
            )
        return client.get_tracer(_TRACER_NAME)


def _to_ns(value: Any) -> int | None:
    """`datetime` -> nanoseconds since epoch (the unit OTel spans expect)."""
    if value is None:
        return None
    # All Trace timestamps are timezone-aware (utc_now()).
    return int(value.timestamp() * 1_000_000_000)


def _trace_attributes(trace: Trace, run_id: str | None) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "evalh.run_id": run_id or trace.run_id,
        "evalh.case_id": trace.case_id,
        "evalh.variant_name": trace.variant_name,
        "evalh.latency_ms": int(trace.latency_ms),
    }
    metrics = trace.metrics
    if metrics.token_input is not None:
        attrs["evalh.metrics.token_input"] = int(metrics.token_input)
    if metrics.token_output is not None:
        attrs["evalh.metrics.token_output"] = int(metrics.token_output)
    if metrics.token_thinking is not None:
        attrs["evalh.metrics.token_thinking"] = int(metrics.token_thinking)
    if metrics.cost_usd is not None:
        attrs["evalh.metrics.cost_usd"] = float(metrics.cost_usd)
    if trace.output.final_answer is not None:
        attrs["evalh.output.final_answer"] = trace.output.final_answer
    return attrs


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


async def _empty_iter() -> AsyncIterator[Any]:
    if False:
        yield  # pragma: no cover — keep the async-generator shape
