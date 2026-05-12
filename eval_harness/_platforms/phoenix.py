"""Phoenix platform helper.

Phoenix is OTel-native: span emission goes through `OtelClient`
(`eval_harness._platforms.otel`) exactly like any other OTLP backend.
Phoenix's read side is a small REST query API that this helper wraps so
the Phoenix DatasetAdapter and TraceEnricher don't each ship their own
HTTP plumbing.

The helper does two jobs:

1. Translate `(base_url, headers, project_name, resource_attributes)` into
   the (OTel endpoint, OTel resource attrs) tuple `OtelClient` wants.
   Phoenix's OTLP collector lives at `<base_url>/v1/traces`; the
   ``openinference.project.name`` resource attribute is the conventional
   way Phoenix groups spans into a project.
2. Provide `fetch_trace(trace_id, *, wait_for_ingestion_seconds, ...)`
   and `search_traces(filter)` over Phoenix's HTTP API for the enricher
   and dataset adapter. Both use httpx and an injectable clock / sleeper
   so tests stay deterministic — no `time.sleep`, no real network.

Sharing pattern mirrors `OtelClient`: callers acquire via
`get_or_create_phoenix_client(...)` and release via
`release_phoenix_client(...)`; same target -> same instance.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Coroutine
from threading import Lock
from types import TracebackType
from typing import Any, Self

import httpx

from eval_harness._platforms.otel import (
    OtelClient,
    get_or_create_otel_client,
    release_otel_client,
)
from eval_harness.core.errors import ConfigError

# Phoenix's OTel collector path under its base URL.
_OTLP_PATH = "/v1/traces"
# Phoenix's REST API for trace queries.
_TRACE_PATH = "/v1/traces/{trace_id}"
_SEARCH_PATH = "/v1/spans"

ClockFn = Callable[[], float]
SleeperFn = Callable[[float], Coroutine[Any, Any, None]]


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


_REGISTRY: dict[str, _Entry] = {}
_REGISTRY_LOCK = Lock()


class _Entry:
    __slots__ = ("client", "refcount")

    def __init__(self, client: PhoenixClient) -> None:
        self.client = client
        self.refcount = 0


def _fingerprint(
    base_url: str,
    api_key: str | None,
    project_name: str | None,
    headers: dict[str, str] | None,
) -> str:
    return json.dumps(
        {
            "base_url": base_url.rstrip("/"),
            "api_key": api_key,
            "project_name": project_name,
            "headers": dict(sorted((headers or {}).items())),
        },
        sort_keys=True,
    )


def phoenix_to_otel_endpoint(base_url: str) -> str:
    """`http://phoenix:6006` -> `http://phoenix:6006/v1/traces`."""
    return base_url.rstrip("/") + _OTLP_PATH


def phoenix_resource_attributes(
    *,
    project_name: str | None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Conventional resource attributes for Phoenix span grouping.

    `openinference.project.name` is the key the Phoenix UI uses to bucket
    spans into a project. We merge any caller-supplied attrs so users can
    override or add their own (e.g. ``deployment.environment``)."""
    attrs: dict[str, str] = {}
    if project_name:
        attrs["openinference.project.name"] = project_name
    if extra:
        attrs.update(extra)
    return attrs


class PhoenixClient:
    """Thin facade around Phoenix's OTel push + REST read APIs.

    The push side delegates to `OtelClient` (shared registry from ev-joq)
    so two Phoenix-shaped adapters in the same run land on one
    `TracerProvider`. The read side is a plain httpx client driven by
    injectable clock + sleeper for deterministic ingestion-lag tests.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        project_name: str | None = None,
        headers: dict[str, str] | None = None,
        resource_attributes: dict[str, str] | None = None,
        # Test seams: callers can inject a pre-built OtelClient and / or a
        # respx-mocked httpx client; production callers leave both None.
        _otel_client: OtelClient | None = None,
        _http_client: httpx.AsyncClient | None = None,
        clock: ClockFn | None = None,
        sleeper: SleeperFn | None = None,
    ) -> None:
        if not base_url:
            raise ConfigError(
                "PhoenixClient: 'base_url' is required "
                "(e.g. 'http://phoenix:6006')"
            )
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.project_name = project_name
        merged_headers: dict[str, str] = dict(headers or {})
        if api_key and "Authorization" not in merged_headers:
            merged_headers["Authorization"] = f"Bearer {api_key}"
        self.headers = merged_headers
        self.resource_attributes = phoenix_resource_attributes(
            project_name=project_name, extra=resource_attributes
        )

        self._otel_owned = _otel_client is None
        self._otel_client: OtelClient | None
        if _otel_client is not None:
            self._otel_client = _otel_client
        else:
            self._otel_client = get_or_create_otel_client(
                endpoint=phoenix_to_otel_endpoint(self.base_url),
                headers=self.headers,
                resource_attributes=self.resource_attributes,
            )

        self._http_owned = _http_client is None
        self._http_client: httpx.AsyncClient = _http_client or httpx.AsyncClient(
            base_url=self.base_url,
            headers=self.headers,
            timeout=10.0,
        )
        self._clock: ClockFn = clock or time.monotonic
        self._sleep: SleeperFn = sleeper or _real_sleep
        self._shutdown_called = False

    @property
    def otel_client(self) -> OtelClient:
        if self._otel_client is None:
            raise RuntimeError("PhoenixClient used after shutdown")
        return self._otel_client

    async def fetch_trace(
        self,
        trace_id: str,
        *,
        wait_for_ingestion_seconds: float = 0.0,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any] | None:
        """Poll Phoenix for a single trace. Returns the trace JSON if found
        within `wait_for_ingestion_seconds`, else None. The 404 path is
        treated as "not yet ingested" and retried until the deadline."""
        if self._shutdown_called:
            raise RuntimeError("PhoenixClient.fetch_trace called after shutdown")
        deadline = self._clock() + max(wait_for_ingestion_seconds, 0.0)
        url = _TRACE_PATH.format(trace_id=trace_id)
        while True:
            try:
                resp = await self._http_client.get(url)
            except httpx.HTTPError:
                resp = None
            if resp is not None and resp.status_code == 200:
                payload = resp.json()
                # Phoenix nests the trace under {"data": ...} on some
                # versions; unwrap if present.
                if isinstance(payload, dict) and "data" in payload and len(payload) == 1:
                    payload = payload["data"]
                return payload if isinstance(payload, dict) else {"raw": payload}
            if self._clock() >= deadline:
                return None
            await self._sleep(poll_interval_seconds)

    async def search_traces(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        """Page through Phoenix's span search for the configured project.

        `filter` is forwarded to the server as query params; callers are
        responsible for using the keys the Phoenix REST API actually
        understands (typically `project_name`, `start_time`, `end_time`,
        `limit`).
        """
        if self._shutdown_called:
            raise RuntimeError("PhoenixClient.search_traces called after shutdown")
        params = dict(filter)
        if self.project_name and "project_name" not in params:
            params["project_name"] = self.project_name
        resp = await self._http_client.get(_SEARCH_PATH, params=params)
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict):
            data = body.get("data")
            if isinstance(data, list):
                return [d for d in data if isinstance(d, dict)]
            if isinstance(data, dict):
                return [data]
        if isinstance(body, list):
            return [d for d in body if isinstance(d, dict)]
        return []

    async def aclose(self) -> None:
        """Drain the HTTP client. Spans go through the OtelClient lifecycle
        in `release_phoenix_client`."""
        if self._http_owned:
            with contextlib.suppress(Exception):
                await self._http_client.aclose()

    def shutdown(self) -> None:
        """Idempotent. Releases the OtelClient back to its shared registry.
        The HTTP client is drained in `aclose` because that's coroutine-only."""
        if self._shutdown_called:
            return
        self._shutdown_called = True
        if self._otel_owned and self._otel_client is not None:
            release_otel_client(self._otel_client)
            self._otel_client = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()
        self.shutdown()


def get_or_create_phoenix_client(
    *,
    base_url: str,
    api_key: str | None = None,
    project_name: str | None = None,
    headers: dict[str, str] | None = None,
    resource_attributes: dict[str, str] | None = None,
    _otel_client: OtelClient | None = None,
    _http_client: httpx.AsyncClient | None = None,
    clock: ClockFn | None = None,
    sleeper: SleeperFn | None = None,
) -> PhoenixClient:
    """Acquire a shared PhoenixClient for `(base_url, api_key, project,
    headers)`. Bumps the refcount; pair every call with
    `release_phoenix_client`. Identical configs share one underlying
    `OtelClient` (and therefore one `TracerProvider`) — that's the
    sharing-with-OTel invariant the spec calls out."""
    key = _fingerprint(base_url, api_key, project_name, headers)
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None:
            client = PhoenixClient(
                base_url=base_url,
                api_key=api_key,
                project_name=project_name,
                headers=headers,
                resource_attributes=resource_attributes,
                _otel_client=_otel_client,
                _http_client=_http_client,
                clock=clock,
                sleeper=sleeper,
            )
            entry = _Entry(client=client)
            _REGISTRY[key] = entry
        entry.refcount += 1
        return entry.client


def release_phoenix_client(client: PhoenixClient) -> None:
    """Release a client previously acquired via `get_or_create_phoenix_client`.
    Last release drains the HTTP client (best-effort, fire-and-forget) and
    releases the underlying OtelClient back to its registry."""
    key = _fingerprint(
        client.base_url, client.api_key, client.project_name, client.headers
    )
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None or entry.client is not client:
            return
        entry.refcount -= 1
        if entry.refcount <= 0:
            _REGISTRY.pop(key, None)
            client.shutdown()


def _clear_registry_for_tests() -> None:
    """Test helper: nuke the registry. Pair with adapter teardown in tests
    that exercise refcount paths directly."""
    with _REGISTRY_LOCK:
        entries = list(_REGISTRY.values())
        _REGISTRY.clear()
    for e in entries:
        with contextlib.suppress(Exception):
            e.client.shutdown()


def _registry_snapshot() -> dict[str, int]:
    with _REGISTRY_LOCK:
        return {k: v.refcount for k, v in _REGISTRY.items()}


__all__ = [
    "PhoenixClient",
    "get_or_create_phoenix_client",
    "phoenix_resource_attributes",
    "phoenix_to_otel_endpoint",
    "release_phoenix_client",
]
