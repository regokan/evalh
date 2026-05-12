"""Arize platform helper.

Arize is OTel-native — span emission goes through `OtelClient`
(`eval_harness._platforms.otel`) exactly like any other OTLP backend, with
Arize's collector endpoint and Arize-conventional resource attributes
(`model_id`, `model_version`, `arize.space_id`). Auth is carried in OTLP
headers (`space_id`, `api_key`).

The helper does two jobs, mirroring the Phoenix helper from ev-cjr:

1. Translate ``(endpoint, space_id, api_key, model_id, model_version,
   environment, headers, resource_attributes)`` into the
   ``(OTel endpoint, OTel headers, OTel resource attrs)`` tuple
   `OtelClient` wants. Default endpoint is ``https://otlp.arize.com/v1``.
2. Provide ``fetch_trace`` and ``search_traces`` over Arize's REST API for
   the enricher and dataset adapter. Both use httpx and an injectable
   clock / sleeper so tests stay deterministic — no `time.sleep`, no real
   network.

Sharing pattern mirrors `OtelClient` / `PhoenixClient`: callers acquire
via `get_or_create_arize_client(...)` and release via
`release_arize_client(...)`; same target -> same instance.
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

# Arize OTLP HTTP collector path under its base URL. The base URL defaults
# to https://otlp.arize.com/v1 but the user can point at a self-hosted
# collector by passing a different ``endpoint``.
_DEFAULT_OTLP_ENDPOINT = "https://otlp.arize.com/v1"
# Conventional Arize REST paths for read-side queries. Real Arize uses
# GraphQL for richer queries; this generic shape is what our fakes mimic.
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

    def __init__(self, client: ArizeClient) -> None:
        self.client = client
        self.refcount = 0


def _fingerprint(
    endpoint: str,
    api_key: str | None,
    space_id: str | None,
    model_id: str | None,
    model_version: str | None,
    environment: str | None,
    headers: dict[str, str] | None,
) -> str:
    return json.dumps(
        {
            "endpoint": endpoint.rstrip("/"),
            "api_key": api_key,
            "space_id": space_id,
            "model_id": model_id,
            "model_version": model_version,
            "environment": environment,
            "headers": dict(sorted((headers or {}).items())),
        },
        sort_keys=True,
    )


def arize_resource_attributes(
    *,
    model_id: str | None,
    model_version: str | None = None,
    space_id: str | None = None,
    environment: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Arize-conventional resource attributes.

    `model_id` is mandatory in production; this helper is permissive so
    callers can build a partial config in tests and validate later. The
    OTel Resource then groups spans into the Arize model identified by
    these attributes.
    """
    attrs: dict[str, str] = {}
    if model_id:
        attrs["model_id"] = model_id
    if model_version:
        attrs["model_version"] = model_version
    if space_id:
        attrs["arize.space_id"] = space_id
    if environment:
        attrs["deployment.environment"] = environment
    if extra:
        attrs.update(extra)
    return attrs


def arize_otel_headers(
    *,
    space_id: str | None,
    api_key: str | None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """OTLP exporter headers Arize expects for auth + space scoping.

    Arize accepts these as gRPC metadata or HTTP headers depending on the
    OTLP protocol; the values are identical either way.
    """
    headers: dict[str, str] = {}
    if space_id:
        headers["space_id"] = space_id
    if api_key:
        headers["api_key"] = api_key
    if extra:
        headers.update(extra)
    return headers


class ArizeClient:
    """Thin facade around Arize's OTel push + REST read APIs.

    The push side delegates to `OtelClient` (shared registry from ev-joq)
    so two Arize-shaped adapters in the same run land on one
    `TracerProvider`. The read side is a plain httpx client driven by
    injectable clock + sleeper for deterministic ingestion-lag tests.
    """

    def __init__(
        self,
        *,
        endpoint: str = _DEFAULT_OTLP_ENDPOINT,
        api_key: str | None = None,
        space_id: str | None = None,
        model_id: str | None = None,
        model_version: str | None = None,
        environment: str | None = None,
        headers: dict[str, str] | None = None,
        resource_attributes: dict[str, str] | None = None,
        protocol: str = "http",
        # Test seams: inject a pre-built OtelClient and/or a respx-mocked
        # httpx client; production callers leave both None.
        _otel_client: OtelClient | None = None,
        _http_client: httpx.AsyncClient | None = None,
        clock: ClockFn | None = None,
        sleeper: SleeperFn | None = None,
    ) -> None:
        if not endpoint:
            raise ConfigError(
                "ArizeClient: 'endpoint' is required "
                "(default https://otlp.arize.com/v1)"
            )
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.space_id = space_id
        self.model_id = model_id
        self.model_version = model_version
        self.environment = environment
        self.protocol = protocol
        # Auth + scope headers Arize expects on OTLP and REST traffic.
        merged_headers: dict[str, str] = arize_otel_headers(
            space_id=space_id, api_key=api_key, extra=headers
        )
        self.headers = merged_headers
        self.resource_attributes = arize_resource_attributes(
            model_id=model_id,
            model_version=model_version,
            space_id=space_id,
            environment=environment,
            extra=resource_attributes,
        )

        self._otel_owned = _otel_client is None
        self._otel_client: OtelClient | None
        if _otel_client is not None:
            self._otel_client = _otel_client
        else:
            self._otel_client = get_or_create_otel_client(
                endpoint=self.endpoint,
                headers=self.headers,
                protocol=protocol,
                resource_attributes=self.resource_attributes,
            )

        # REST base URL is the same root as the OTLP endpoint by default;
        # callers can override via headers/explicit injection if a separate
        # API endpoint is needed.
        rest_base = _rest_base_from_otlp(self.endpoint)
        self._http_owned = _http_client is None
        self._http_client: httpx.AsyncClient = _http_client or httpx.AsyncClient(
            base_url=rest_base,
            headers=self.headers,
            timeout=10.0,
        )
        self._clock: ClockFn = clock or time.monotonic
        self._sleep: SleeperFn = sleeper or _real_sleep
        self._shutdown_called = False

    @property
    def otel_client(self) -> OtelClient:
        if self._otel_client is None:
            raise RuntimeError("ArizeClient used after shutdown")
        return self._otel_client

    async def fetch_trace(
        self,
        trace_id: str,
        *,
        wait_for_ingestion_seconds: float = 0.0,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any] | None:
        """Poll Arize for a single trace. Returns the trace JSON if found
        within `wait_for_ingestion_seconds`, else None. 404 is treated as
        "not yet ingested" and retried until the deadline."""
        if self._shutdown_called:
            raise RuntimeError("ArizeClient.fetch_trace called after shutdown")
        deadline = self._clock() + max(wait_for_ingestion_seconds, 0.0)
        url = _TRACE_PATH.format(trace_id=trace_id)
        while True:
            try:
                resp = await self._http_client.get(url)
            except httpx.HTTPError:
                resp = None
            if resp is not None and resp.status_code == 200:
                payload = resp.json()
                if (
                    isinstance(payload, dict)
                    and "data" in payload
                    and len(payload) == 1
                ):
                    payload = payload["data"]
                return payload if isinstance(payload, dict) else {"raw": payload}
            if self._clock() >= deadline:
                return None
            await self._sleep(poll_interval_seconds)

    async def search_traces(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        """Search Arize for traces matching `filter`.

        `filter` is forwarded as query params; production callers should
        use keys Arize's REST API understands (e.g. ``model_id``,
        ``start_time``, ``end_time``, ``limit``). When the caller doesn't
        pass `model_id` we fill it from the client's config so server-side
        scoping still works.
        """
        if self._shutdown_called:
            raise RuntimeError("ArizeClient.search_traces called after shutdown")
        params = dict(filter)
        if self.model_id and "model_id" not in params:
            params["model_id"] = self.model_id
        if self.space_id and "space_id" not in params:
            params["space_id"] = self.space_id
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
        """Drain the HTTP client. Span lifecycle lives on the OtelClient
        (released in `shutdown`)."""
        if self._http_owned:
            with contextlib.suppress(Exception):
                await self._http_client.aclose()

    def shutdown(self) -> None:
        """Idempotent. Releases the OtelClient back to its shared registry."""
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


def _rest_base_from_otlp(otlp_endpoint: str) -> str:
    """Strip the OTLP traces path so the REST httpx client lands on the
    same Arize host root."""
    base = otlp_endpoint.rstrip("/")
    for suffix in ("/v1/traces", "/v1"):
        if base.endswith(suffix):
            return base[: -len(suffix)] or base
    return base


def get_or_create_arize_client(
    *,
    endpoint: str = _DEFAULT_OTLP_ENDPOINT,
    api_key: str | None = None,
    space_id: str | None = None,
    model_id: str | None = None,
    model_version: str | None = None,
    environment: str | None = None,
    headers: dict[str, str] | None = None,
    resource_attributes: dict[str, str] | None = None,
    protocol: str = "http",
    _otel_client: OtelClient | None = None,
    _http_client: httpx.AsyncClient | None = None,
    clock: ClockFn | None = None,
    sleeper: SleeperFn | None = None,
) -> ArizeClient:
    """Acquire a shared ArizeClient. Same-config callers share one
    underlying `OtelClient` (and therefore one `TracerProvider`)."""
    key = _fingerprint(
        endpoint, api_key, space_id, model_id, model_version, environment, headers
    )
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None:
            client = ArizeClient(
                endpoint=endpoint,
                api_key=api_key,
                space_id=space_id,
                model_id=model_id,
                model_version=model_version,
                environment=environment,
                headers=headers,
                resource_attributes=resource_attributes,
                protocol=protocol,
                _otel_client=_otel_client,
                _http_client=_http_client,
                clock=clock,
                sleeper=sleeper,
            )
            entry = _Entry(client=client)
            _REGISTRY[key] = entry
        entry.refcount += 1
        return entry.client


def release_arize_client(client: ArizeClient) -> None:
    """Release a client previously acquired via `get_or_create_arize_client`.
    Last release drains the HTTP client (best-effort) and releases the
    underlying OtelClient."""
    key = _fingerprint(
        client.endpoint,
        client.api_key,
        client.space_id,
        client.model_id,
        client.model_version,
        client.environment,
        client.headers,
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
    """Test helper: nuke the registry."""
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
    "ArizeClient",
    "arize_otel_headers",
    "arize_resource_attributes",
    "get_or_create_arize_client",
    "release_arize_client",
]
