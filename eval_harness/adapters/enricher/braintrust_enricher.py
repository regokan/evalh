"""Braintrust TraceEnricher.

Fetches the upstream Braintrust trace by ``Trace.extra.trace_id`` and
merges selected fields onto our local trace.

Failure-soft (per ev-sa7 + ``docs/Adapters.md`` > TraceEnricher): this
adapter *raises* on any failure (timeout, ingestion miss, malformed
upstream). The runner catches and records
``{enricher, error}`` into ``trace.extra.enrichment_errors``; the cell
continues with the un-enriched trace.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

from jsonpath_ng.ext import parse as jsonpath_parse

from eval_harness._platforms.braintrust import (
    BraintrustClient,
    ClockFn,
    SleeperFn,
    get_or_create_braintrust_client,
    release_braintrust_client,
)
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import Trace


class BraintrustTraceEnricher:
    """Polls Braintrust for ``Trace.extra.trace_id`` and merges fields
    into the local Trace per the configured `merge` rules."""

    name: str = "braintrust_enricher"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        project: str | None = None,
        org: str | None = None,
        wait_for_ingestion_seconds: float = 2.0,
        poll_interval_seconds: float = 0.5,
        merge: dict[str, str] | None = None,
        client: BraintrustClient | None = None,
        clock: ClockFn | None = None,
        sleeper: SleeperFn | None = None,
        **_extra: Any,
    ) -> None:
        if not isinstance(merge, dict) or not merge:
            raise ConfigError(
                "braintrust enricher: 'merge' (dict[target -> jsonpath]) is required"
            )
        self._merge_spec = _compile_merge(merge)
        self._wait = float(wait_for_ingestion_seconds)
        self._poll_interval = float(poll_interval_seconds)
        self._owns_client = client is None
        self._client: BraintrustClient = client or get_or_create_braintrust_client(
            api_key=api_key, project=project, org=org, clock=clock, sleeper=sleeper
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
            release_braintrust_client(self._client)

    async def enrich(self, trace: Trace) -> Trace:
        trace_id = _extract_trace_id(trace)
        if trace_id is None:
            raise AdapterError(
                "braintrust enricher: Trace.extra.trace_id missing — the "
                "SystemAdapter's response_mapping must extract a trace_id"
            )
        upstream = await self._client.fetch_trace(
            trace_id,
            wait_for_ingestion_seconds=self._wait,
            poll_interval_seconds=self._poll_interval,
        )
        if upstream is None:
            raise AdapterError(
                f"braintrust enricher: upstream trace {trace_id!r} did not "
                f"appear within {self._wait}s (ingestion lag or wrong id)"
            )
        _apply_merge(trace, upstream, self._merge_spec)
        return trace


def _extract_trace_id(trace: Trace) -> str | None:
    raw = trace.extra.get("trace_id")
    return raw if isinstance(raw, str) and raw else None


def _compile_merge(merge: dict[str, str]) -> list[tuple[str, Any]]:
    compiled: list[tuple[str, Any]] = []
    for target, expr in merge.items():
        if not isinstance(target, str) or not target:
            raise ConfigError(
                f"braintrust enricher: merge key must be a non-empty string; got {target!r}"
            )
        if not isinstance(expr, str) or not expr:
            raise ConfigError(
                f"braintrust enricher: merge[{target!r}] must be a non-empty JSONPath string"
            )
        try:
            parsed = jsonpath_parse(expr)
        except Exception as e:
            raise ConfigError(
                f"braintrust enricher: merge[{target!r}] is not a valid JSONPath: {e}"
            ) from e
        compiled.append((target, parsed))
    return compiled


def _apply_merge(
    trace: Trace, upstream: dict[str, Any], rules: list[tuple[str, Any]]
) -> None:
    warnings: list[dict[str, str]] = []
    for target, parsed in rules:
        matches = [m.value for m in parsed.find(upstream)]
        if not matches:
            warnings.append({"target": target, "reason": "jsonpath matched nothing"})
            continue
        value: Any = matches[0] if len(matches) == 1 else matches
        try:
            _assign_path(trace, target, value)
        except (AttributeError, KeyError, TypeError) as exc:
            warnings.append({"target": target, "reason": f"{type(exc).__name__}: {exc}"})
    if warnings:
        existing = trace.extra.setdefault("enrichment_warnings", [])
        existing.extend(warnings)


def _assign_path(trace: Trace, dotted: str, value: Any) -> None:
    parts = dotted.split(".")
    if len(parts) == 1:
        setattr(trace, parts[0], value)
        return
    obj: Any = trace
    for part in parts[:-1]:
        if isinstance(obj, dict):
            obj = obj.setdefault(part, {})
        else:
            nxt = getattr(obj, part, None)
            if nxt is None:
                raise AttributeError(f"trace has no field {part!r} on {obj!r}")
            obj = nxt
    leaf = parts[-1]
    if isinstance(obj, dict):
        obj[leaf] = value
    else:
        setattr(obj, leaf, value)
