"""Langfuse DatasetAdapter — pull cases from production observability.

Maps upstream Langfuse traces to `EvalCase`. With ``embed_full_trace=true``
each case's ``_embedded_trace`` carries the original `Trace`, so the v1
``replay`` SystemAdapter can score what already happened (per docs/Adapters
> "v1: replay" + ev-s95).

Auth + connection live in `eval_harness._platforms.langfuse.LangfuseClient`
— this file is just shape mapping.
"""

from __future__ import annotations

import contextlib
import random
from typing import Any

from eval_harness._platforms.langfuse import (
    LangfuseClient,
    get_or_create_langfuse_client,
    release_langfuse_client,
)
from eval_harness.core.errors import AdapterError
from eval_harness.core.models import EvalCase, Trace


class LangfuseDatasetAdapter:
    embed_full_trace: bool

    def __init__(
        self,
        *,
        api_key: str | None = None,
        host: str | None = None,
        filter: dict[str, Any] | None = None,
        sample: int | None = None,
        embed_full_trace: bool = False,
        client: LangfuseClient | None = None,
        **_extra: Any,
    ) -> None:
        self._filter: dict[str, Any] = dict(filter or {})
        self._sample = sample
        self.embed_full_trace = embed_full_trace
        self._owns_client = client is None
        self._client: LangfuseClient = client or get_or_create_langfuse_client(
            api_key=api_key, host=host
        )

    async def load_cases(self) -> list[EvalCase]:
        try:
            payloads = await self._client.search_traces(self._filter)
        except Exception as e:
            raise AdapterError(
                f"langfuse dataset: search_traces failed: {type(e).__name__}: {e}"
            ) from e

        cases: list[EvalCase] = []
        for raw in payloads:
            cases.append(self._build_case(raw))

        if self._sample is not None and self._sample < len(cases):
            # Seeded sample so the same filter on the same dataset reproduces.
            rng = random.Random(repr(sorted(self._filter.items())))
            cases = rng.sample(cases, self._sample)
        return cases

    def __del__(self) -> None:  # pragma: no cover — best-effort
        if self._owns_client:
            with contextlib.suppress(Exception):
                release_langfuse_client(self._client)

    def _build_case(self, raw: dict[str, Any]) -> EvalCase:
        case_id = str(raw.get("id") or raw.get("trace_id") or raw.get("name") or "")
        if not case_id:
            raise AdapterError(
                f"langfuse dataset: upstream trace has no id-like field: {raw!r}"
            )

        case_input = _extract_input(raw)
        metadata = dict(raw.get("metadata") or {})
        # Stash provenance so evaluators / inspect can render where the case
        # came from.
        metadata.setdefault("source", "langfuse")
        if "trace_id" not in metadata and raw.get("id"):
            metadata["trace_id"] = str(raw["id"])

        case = EvalCase(id=case_id, input=case_input, metadata=metadata)
        if self.embed_full_trace:
            case._embedded_trace = _upstream_trace_to_local(raw, case_id)
        return case


def _extract_input(raw: dict[str, Any]) -> dict[str, Any]:
    """Map the upstream trace's `input` field to our `EvalCase.input` dict.
    Langfuse stores inputs in a few shapes — pick the first that's present."""
    candidate = raw.get("input")
    if isinstance(candidate, dict):
        return dict(candidate)
    if isinstance(candidate, str):
        return {"user_message": candidate}
    # Fall back to a derived field if the upstream trace nests it.
    nested = raw.get("inputs") or raw.get("user_input")
    if isinstance(nested, dict):
        return dict(nested)
    if isinstance(nested, str):
        return {"user_message": nested}
    return {}


def _upstream_trace_to_local(raw: dict[str, Any], case_id: str) -> Trace:
    """Build a local `Trace` from an upstream langfuse payload.

    Field names follow Langfuse's typical shape; missing fields default to
    safe values. Replay preservation requires that ``started_at`` /
    ``finished_at`` / ``latency_ms`` / metrics pass through byte-for-byte
    here so the replay adapter (ev-s95) returns them unchanged — runner's
    ``_enforce_invariants`` skips its overwrite when ``extra.source ==
    'replay'``.
    """

    from eval_harness.core.models import (
        ToolCall,
        ToolResult,
        TraceMessage,
        TraceMetrics,
        TraceOutput,
    )
    from eval_harness.core.time import utc_now

    started = _parse_dt(raw.get("started_at") or raw.get("timestamp")) or utc_now()
    finished = _parse_dt(raw.get("finished_at") or raw.get("end_time")) or started
    latency_raw = raw.get("latency_ms")
    if isinstance(latency_raw, int | float):
        latency_ms = int(latency_raw)
    else:
        latency_ms = max(0, int((finished - started).total_seconds() * 1000))

    output_raw = raw.get("output")
    final_answer: str | None = None
    thinking: str | None = None
    if isinstance(output_raw, dict):
        final_answer = output_raw.get("final_answer") or output_raw.get("answer")
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
    metrics = TraceMetrics.model_validate(metrics_raw) if isinstance(
        metrics_raw, dict
    ) else TraceMetrics()

    extra: dict[str, Any] = {}
    if raw.get("id") or raw.get("trace_id"):
        extra["trace_id"] = str(raw.get("id") or raw.get("trace_id"))
    extra["source_platform"] = "langfuse"

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


def _parse_dt(value: Any) -> Any:
    from datetime import datetime

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
