"""OTel TraceEnricher — fetch upstream spans by `trace_id` and merge them
into our Trace.

Targets any OTel-queryable backend (Tempo, Honeycomb, Grafana, Phoenix in
OTel mode). The user supplies a URL pattern with a `{trace_id}` placeholder
and a `merge` dict mapping target Trace paths to JSONPath expressions on
the fetched response body.

Ingestion-lag handling: bounded retry with `wait_for_ingestion_seconds`
between attempts, capped at `max_attempts`. The clock is injectable for
tests — production callers leave it unset and get real `asyncio.sleep`.

Failure-soft semantics live in the *runner*, not here. This enricher
raises on terminal failure; `runner._run_enrichers` catches and logs to
`trace.extra.enrichment_errors`.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import TracebackType
from typing import Any, Self

import httpx
from jsonpath_ng import parse as jsonpath_parse

from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import Trace

_DEFAULT_TIMEOUT_SECONDS = 10.0
_DEFAULT_INGESTION_LAG_SECONDS = 2.0
_DEFAULT_MAX_ATTEMPTS = 5

SleepFn = Callable[[float], Awaitable[None]]


class OtelTraceEnricher:
    """Polls an OTel query API by `trace_id` and merges the response into
    our Trace per the `merge` rules.

    The trace_id is read from `trace.extra["trace_id"]` (set by the
    SystemAdapter's `response_mapping`). When absent, this enricher raises
    `AdapterError` — the runner records it as a soft enrichment failure.
    """

    name: str

    def __init__(
        self,
        name: str = "otel",
        *,
        endpoint: str | None = None,
        headers: dict[str, str] | None = None,
        wait_for_ingestion_seconds: float = _DEFAULT_INGESTION_LAG_SECONDS,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        request_timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
        merge: dict[str, str] | None = None,
        trace_id_field: str = "trace_id",
        # Test seam: caller-supplied httpx client + sleeper. Production
        # leaves both None.
        _http_client: httpx.AsyncClient | None = None,
        _sleep: SleepFn | None = None,
        **_extra: Any,
    ) -> None:
        if not endpoint:
            raise ConfigError(
                "otel enricher requires 'endpoint' — typically a URL pattern "
                "with `{trace_id}`, e.g. 'http://tempo:3200/api/traces/{trace_id}'"
            )
        if "{trace_id}" not in endpoint:
            raise ConfigError(
                "otel enricher: 'endpoint' must contain `{trace_id}` placeholder"
            )
        if max_attempts < 1:
            raise ConfigError(
                "otel enricher: 'max_attempts' must be >= 1"
            )
        if wait_for_ingestion_seconds < 0:
            raise ConfigError(
                "otel enricher: 'wait_for_ingestion_seconds' must be >= 0"
            )
        self.name = name
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.wait_for_ingestion_seconds = float(wait_for_ingestion_seconds)
        self.max_attempts = int(max_attempts)
        self.request_timeout_seconds = float(request_timeout_seconds)
        self.merge_rules = _compile_merge_rules(merge or {})
        self.trace_id_field = trace_id_field
        self._injected_http_client = _http_client
        self._owns_http_client = _http_client is None
        self._http_client: httpx.AsyncClient | None = _http_client
        self._sleep: SleepFn = _sleep or asyncio.sleep

    async def __aenter__(self) -> Self:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                headers=self.headers, timeout=self.request_timeout_seconds
            )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_http_client and self._http_client is not None:
            await self._http_client.aclose()
            self._http_client = None

    async def enrich(self, trace: Trace) -> Trace:
        trace_id = trace.extra.get(self.trace_id_field)
        if not isinstance(trace_id, str) or not trace_id:
            raise AdapterError(
                f"otel enricher: trace.extra[{self.trace_id_field!r}] missing or "
                "not a string; cannot resolve upstream trace"
            )
        url = self.endpoint.replace("{trace_id}", trace_id)
        client = self._http_client
        if client is None:
            raise RuntimeError(
                "OtelTraceEnricher.enrich called outside the `async with` context"
            )

        last_error: str | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await client.get(url)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if resp.status_code == 200:
                    payload = resp.json()
                    _apply_merge(trace, payload, self.merge_rules)
                    return trace
                # 404 typically means "not ingested yet" — retry. Other 4xx
                # are caller errors (bad token, bad URL); 5xx are transient.
                # Retry both up to max_attempts; the bounded loop limits cost.
                last_error = f"HTTP {resp.status_code}"
            if attempt < self.max_attempts:
                await self._sleep(self.wait_for_ingestion_seconds)

        raise AdapterError(
            f"otel enricher: gave up after {self.max_attempts} attempts on "
            f"{url} (last: {last_error})"
        )


def _compile_merge_rules(merge: dict[str, str]) -> list[tuple[str, Any]]:
    """Validate at __init__ time so a bad expression fails the run at plan
    time, not on the first cell."""
    out: list[tuple[str, Any]] = []
    for target, expr in merge.items():
        if not isinstance(target, str) or not target:
            raise ConfigError(f"otel enricher: merge key must be a non-empty string; got {target!r}")
        if not isinstance(expr, str) or not expr:
            raise ConfigError(
                f"otel enricher: merge[{target!r}] must be a non-empty JSONPath string"
            )
        try:
            parsed = jsonpath_parse(expr)
        except Exception as e:
            raise ConfigError(
                f"otel enricher: merge[{target!r}] is not a valid JSONPath: {e}"
            ) from e
        out.append((target, parsed))
    return out


def _apply_merge(trace: Trace, payload: Any, rules: list[tuple[str, Any]]) -> None:
    """Apply each (target, compiled_jsonpath) rule to the fetched payload.

    Target paths use dotted notation against the Trace's pydantic model:
    `tool_calls`, `messages`, `metrics.token_input`, etc. Unrecognized
    targets are surfaced as `trace.extra.enrichment_warnings`.
    """
    warnings: list[dict[str, str]] = []
    for target, parsed in rules:
        matches = parsed.find(payload)
        if not matches:
            warnings.append(
                {"target": target, "reason": "jsonpath matched nothing"}
            )
            continue
        # Multi-match collects values into a list; single-match unwraps.
        values = [m.value for m in matches]
        value = values if len(values) > 1 else values[0]
        try:
            _set_nested(trace, target, value)
        except (AttributeError, KeyError, TypeError) as exc:
            warnings.append({"target": target, "reason": f"{type(exc).__name__}: {exc}"})
    if warnings:
        existing = trace.extra.setdefault("enrichment_warnings", [])
        existing.extend(warnings)


def _set_nested(trace: Trace, dotted: str, value: Any) -> None:
    """Apply a dotted path against the Trace's pydantic field tree.

    Top-level pydantic fields can be assigned directly; nested model fields
    (e.g. `metrics.token_input`) walk into the model and assign the leaf.
    """
    parts = dotted.split(".")
    parent: Any = trace
    for part in parts[:-1]:
        nxt = getattr(parent, part, None)
        if nxt is None:
            raise AttributeError(f"trace has no field {part!r} on {parent!r}")
        parent = nxt
    leaf = parts[-1]
    setattr(parent, leaf, value)
