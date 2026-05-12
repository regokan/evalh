"""Langfuse TraceEnricher.

Fetches the upstream Langfuse trace by ``Trace.extra.trace_id`` and merges
selected fields onto our local trace.

**Failure-soft contract** (per ev-sa7 + ``docs/Adapters.md`` > TraceEnricher):
this enricher *raises* on any failure (timeout, ingestion miss, malformed
upstream). The runner catches and records ``{enricher, error}`` into
``trace.extra.enrichment_errors``; the cell continues with the un-enriched
trace. Production-observability hiccups stay out of pass/fail.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

from jsonpath_ng.ext import parse as jsonpath_parse

from eval_harness._platforms.langfuse import (
    ClockFn,
    LangfuseClient,
    SleeperFn,
    get_or_create_langfuse_client,
    release_langfuse_client,
)
from eval_harness.core.errors import AdapterError
from eval_harness.core.models import Trace


class LangfuseTraceEnricher:
    """Fetches an upstream trace and merges a configurable subset of fields
    onto the local trace.

    Config:
      - ``api_key``, ``host`` — passed through to LangfuseClient.
      - ``wait_for_ingestion_seconds`` (default 2) — how long to poll for
        the upstream trace before giving up.
      - ``merge`` (dict) — ``{ <target_field>: <jsonpath into upstream> }``.
        Target fields are dotted paths into our local Trace (e.g.
        ``"messages"``, ``"metrics.token_input"``, ``"extra.upstream_score"``).
        JSONPath is evaluated on the upstream payload.
    """

    name: str = "langfuse_enricher"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        host: str | None = None,
        wait_for_ingestion_seconds: float = 2.0,
        merge: dict[str, str] | None = None,
        client: LangfuseClient | None = None,
        clock: ClockFn | None = None,
        sleeper: SleeperFn | None = None,
        **_extra: Any,
    ) -> None:
        if not isinstance(merge, dict) or not merge:
            raise AdapterError(
                "langfuse enricher: 'merge' (dict[target -> jsonpath]) "
                "is required"
            )
        self._merge_spec: dict[str, str] = dict(merge)
        self._wait = float(wait_for_ingestion_seconds)
        self._owns_client = client is None
        self._client: LangfuseClient = client or get_or_create_langfuse_client(
            api_key=api_key, host=host, clock=clock, sleeper=sleeper
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client:
            release_langfuse_client(self._client)

    async def enrich(self, trace: Trace) -> Trace:
        trace_id = _extract_trace_id(trace)
        if trace_id is None:
            raise AdapterError(
                "langfuse enricher: Trace.extra.trace_id is missing — the "
                "SystemAdapter's response_mapping must extract a trace_id "
                "for this enricher to fetch the upstream trace"
            )

        upstream = await self._client.fetch_trace(
            trace_id, wait_for_ingestion_seconds=self._wait
        )
        if upstream is None:
            raise AdapterError(
                f"langfuse enricher: upstream trace '{trace_id}' did not "
                f"appear within {self._wait}s (ingestion lag or wrong id)"
            )

        _apply_merge(trace, upstream, self._merge_spec)
        return trace


def _extract_trace_id(trace: Trace) -> str | None:
    raw = trace.extra.get("trace_id")
    return raw if isinstance(raw, str) and raw else None


def _apply_merge(
    trace: Trace, upstream: dict[str, Any], merge_spec: dict[str, str]
) -> None:
    """For each ``target_field -> jsonpath`` rule, evaluate the JSONPath on
    the upstream payload and write the result into the local Trace.

    Targets are dotted paths rooted at the Trace. Single-segment paths
    write a top-level Trace field; multi-segment paths walk into models
    (``output.thinking``, ``metrics.token_input``) and dict-typed slots
    (``extra.upstream_score``).
    """
    for target, jsonpath_expr in merge_spec.items():
        try:
            jp = jsonpath_parse(jsonpath_expr)
        except Exception as e:
            raise AdapterError(
                f"langfuse enricher: invalid JSONPath in merge[{target!r}]: "
                f"{jsonpath_expr!r}: {e}"
            ) from e
        matches = [m.value for m in jp.find(upstream)]
        if not matches:
            continue
        value: Any = matches[0] if len(matches) == 1 else matches
        _assign_path(trace, target, value)


def _assign_path(trace: Trace, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    if len(parts) == 1:
        # Top-level Trace field. Use setattr so Pydantic still validates.
        setattr(trace, parts[0], value)
        return
    obj: Any = trace
    for part in parts[:-1]:
        obj = obj.setdefault(part, {}) if isinstance(obj, dict) else getattr(obj, part)
    leaf = parts[-1]
    if isinstance(obj, dict):
        obj[leaf] = value
    else:
        setattr(obj, leaf, value)
