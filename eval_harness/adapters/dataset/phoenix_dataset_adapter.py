"""Phoenix DatasetAdapter — pull cases from Phoenix's REST API.

Supports `embed_full_trace: true` so the v1 `replay` SystemAdapter can
score historical Phoenix traffic offline. Auth + connection live in
`eval_harness._platforms.phoenix.PhoenixClient`; this file is just shape
mapping (the upstream span/trace JSON -> our `EvalCase` / `Trace`).
"""

from __future__ import annotations

import contextlib
import random
from datetime import datetime
from typing import Any

from eval_harness._platforms.phoenix import (
    PhoenixClient,
    get_or_create_phoenix_client,
    release_phoenix_client,
)
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import (
    EvalCase,
    ToolCall,
    ToolResult,
    Trace,
    TraceMessage,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.core.time import utc_now


class PhoenixDatasetAdapter:
    embed_full_trace: bool

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        project_name: str | None = None,
        headers: dict[str, str] | None = None,
        filter: dict[str, Any] | None = None,
        sample: int | None = None,
        embed_full_trace: bool = False,
        client: PhoenixClient | None = None,
        **_extra: Any,
    ) -> None:
        if client is None and not base_url:
            raise ConfigError(
                "phoenix dataset: 'base_url' is required when no client is injected"
            )
        self._filter: dict[str, Any] = dict(filter or {})
        self._sample = sample
        self.embed_full_trace = embed_full_trace
        self._owns_client = client is None
        self._client: PhoenixClient = client or get_or_create_phoenix_client(
            base_url=base_url or "",
            api_key=api_key,
            project_name=project_name,
            headers=headers,
        )

    async def load_cases(self) -> list[EvalCase]:
        try:
            payloads = await self._client.search_traces(self._filter)
        except Exception as e:
            raise AdapterError(
                f"phoenix dataset: search_traces failed: {type(e).__name__}: {e}"
            ) from e

        cases: list[EvalCase] = [self._build_case(raw) for raw in payloads]

        if self._sample is not None and self._sample < len(cases):
            rng = random.Random(repr(sorted(self._filter.items())))
            cases = rng.sample(cases, self._sample)
        return cases

    def __del__(self) -> None:  # pragma: no cover — best-effort
        # Constructor may have raised before assigning, so guard with getattr.
        if getattr(self, "_owns_client", False):
            with contextlib.suppress(Exception):
                release_phoenix_client(self._client)

    def _build_case(self, raw: dict[str, Any]) -> EvalCase:
        case_id = str(
            raw.get("id")
            or raw.get("trace_id")
            or raw.get("span_id")
            or raw.get("name")
            or ""
        )
        if not case_id:
            raise AdapterError(
                f"phoenix dataset: upstream trace has no id-like field: {raw!r}"
            )
        case_input = _extract_input(raw)
        metadata = dict(raw.get("metadata") or {})
        metadata.setdefault("source", "phoenix")
        if "trace_id" not in metadata and (raw.get("id") or raw.get("trace_id")):
            metadata["trace_id"] = str(raw.get("id") or raw.get("trace_id"))

        case = EvalCase(id=case_id, input=case_input, metadata=metadata)
        if self.embed_full_trace:
            case._embedded_trace = _upstream_trace_to_local(raw, case_id)
        return case


def _extract_input(raw: dict[str, Any]) -> dict[str, Any]:
    """Phoenix stores inputs in a few shapes — `input`, `attributes.input`,
    or `input_value`. Pick the first present and coerce to a dict."""
    for key in ("input", "input_value"):
        candidate = raw.get(key)
        if isinstance(candidate, dict):
            return dict(candidate)
        if isinstance(candidate, str):
            return {"user_message": candidate}
    attrs = raw.get("attributes")
    if isinstance(attrs, dict):
        nested = attrs.get("input") or attrs.get("input.value")
        if isinstance(nested, dict):
            return dict(nested)
        if isinstance(nested, str):
            return {"user_message": nested}
    return {}


def _upstream_trace_to_local(raw: dict[str, Any], case_id: str) -> Trace:
    started = _parse_dt(raw.get("started_at") or raw.get("start_time")) or utc_now()
    finished = _parse_dt(raw.get("finished_at") or raw.get("end_time")) or started
    latency_raw = raw.get("latency_ms")
    if isinstance(latency_raw, int | float):
        latency_ms = int(latency_raw)
    else:
        latency_ms = max(0, int((finished - started).total_seconds() * 1000))

    output_raw = raw.get("output") or (raw.get("attributes") or {}).get("output")
    final_answer: str | None = None
    thinking: str | None = None
    if isinstance(output_raw, dict):
        final_answer = output_raw.get("final_answer") or output_raw.get("answer") or output_raw.get("value")
        thinking = output_raw.get("thinking")
    elif isinstance(output_raw, str):
        final_answer = output_raw

    messages = [
        TraceMessage.model_validate(m)
        for m in (raw.get("messages") or [])
        if isinstance(m, dict)
    ]
    tool_calls = [
        ToolCall.model_validate(t)
        for t in (raw.get("tool_calls") or [])
        if isinstance(t, dict)
    ]
    tool_results = [
        ToolResult.model_validate(t)
        for t in (raw.get("tool_results") or [])
        if isinstance(t, dict)
    ]

    metrics_raw = raw.get("metrics") or {}
    metrics = (
        TraceMetrics.model_validate(metrics_raw)
        if isinstance(metrics_raw, dict)
        else TraceMetrics()
    )

    extra: dict[str, Any] = {"source_platform": "phoenix"}
    if raw.get("id") or raw.get("trace_id"):
        extra["trace_id"] = str(raw.get("id") or raw.get("trace_id"))

    return Trace(
        run_id=str(raw.get("run_id") or ""),
        case_id=case_id,
        variant_name=str(raw.get("variant_name") or "production"),
        started_at=started,
        finished_at=finished,
        latency_ms=latency_ms,
        input=_extract_input(raw),
        output=TraceOutput(final_answer=final_answer, thinking=thinking),
        messages=messages,
        tool_calls=tool_calls,
        tool_results=tool_results,
        metrics=metrics,
        extra=extra,
    )


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None
