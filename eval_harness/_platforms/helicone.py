"""Helicone platform helper.

Helicone is proxy-shaped: it sits between your app and an LLM provider and
records the request/response stream. We use it as a *dataset source* —
pulling historical request logs as eval cases for backtesting or online
evaluation. Push-side (TraceStore) and enrichment (TraceEnricher) aren't
in scope for v1.x; users who want trace-store semantics point Helicone's
OTel exporter at our `otel` TraceStore.

Auth: `Helicone-Auth: Bearer <api_key>` header on every request. No SDK
required — httpx + the REST API cover everything we need, so the
`[helicone]` extra is just a marker.
"""

from __future__ import annotations

import contextlib
import json
from threading import Lock
from typing import Any

import httpx

from eval_harness.core.errors import ConfigError

_DEFAULT_HOST = "https://api.helicone.ai"
_QUERY_PATH = "/v1/request/query"
_FETCH_PATH = "/v1/request/{request_id}"

_REGISTRY: dict[str, _Entry] = {}
_REGISTRY_LOCK = Lock()


class _Entry:
    __slots__ = ("client", "refcount")

    def __init__(self, client: HeliconeClient) -> None:
        self.client = client
        self.refcount = 0


def _fingerprint(host: str, api_key: str | None) -> str:
    return json.dumps({"host": host.rstrip("/"), "api_key": api_key}, sort_keys=True)


class HeliconeClient:
    """Thin facade over Helicone's REST API.

    Two methods, mirroring the DatasetAdapter's needs:
      - ``search_requests(filter)`` -> list[dict]: page of historical
        request logs matching the filter (model, user, time range, ...).
      - ``fetch_request(request_id)`` -> dict | None: single record.
    """

    def __init__(
        self,
        *,
        api_key: str,
        host: str | None = None,
        _http_client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            raise ConfigError(
                "HeliconeClient: 'api_key' is required (set HELICONE_API_KEY or "
                "pass `api_key=...`)"
            )
        self.api_key = api_key
        self.host = (host or _DEFAULT_HOST).rstrip("/")
        self._owns_http = _http_client is None
        self._http_client: httpx.AsyncClient = _http_client or httpx.AsyncClient(
            base_url=self.host,
            headers={
                "Helicone-Auth": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=15.0,
        )
        self._shutdown_called = False

    async def search_requests(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        """POST the filter to /v1/request/query. The Helicone API returns a
        JSON envelope: either ``{"data": [...]}`` or a top-level list."""
        if self._shutdown_called:
            raise RuntimeError("HeliconeClient.search_requests called after shutdown")
        resp = await self._http_client.post(_QUERY_PATH, json=dict(filter))
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

    async def fetch_request(self, request_id: str) -> dict[str, Any] | None:
        if self._shutdown_called:
            raise RuntimeError("HeliconeClient.fetch_request called after shutdown")
        url = _FETCH_PATH.format(request_id=request_id)
        try:
            resp = await self._http_client.get(url)
        except httpx.HTTPError:
            return None
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        body = resp.json()
        if isinstance(body, dict):
            # Unwrap a `{"data": {...}}` envelope.
            if "data" in body and len(body) == 1 and isinstance(body["data"], dict):
                return dict(body["data"])
            return dict(body)
        return None

    async def aclose(self) -> None:
        if self._owns_http:
            with contextlib.suppress(Exception):
                await self._http_client.aclose()

    def shutdown(self) -> None:
        if self._shutdown_called:
            return
        self._shutdown_called = True


def get_or_create_helicone_client(
    *,
    api_key: str,
    host: str | None = None,
    _http_client: httpx.AsyncClient | None = None,
) -> HeliconeClient:
    """Acquire a shared HeliconeClient for `(host, api_key)`. Pair with
    `release_helicone_client`."""
    key = _fingerprint(host or _DEFAULT_HOST, api_key)
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None:
            client = HeliconeClient(
                api_key=api_key, host=host, _http_client=_http_client
            )
            entry = _Entry(client=client)
            _REGISTRY[key] = entry
        entry.refcount += 1
        return entry.client


def release_helicone_client(client: HeliconeClient) -> None:
    """Release a client. Last release runs `shutdown()` and drops the entry."""
    key = _fingerprint(client.host, client.api_key)
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None or entry.client is not client:
            return
        entry.refcount -= 1
        if entry.refcount <= 0:
            _REGISTRY.pop(key, None)
            client.shutdown()


def _clear_registry_for_tests() -> None:
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
    "HeliconeClient",
    "get_or_create_helicone_client",
    "release_helicone_client",
]
