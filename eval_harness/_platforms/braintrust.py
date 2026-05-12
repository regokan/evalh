"""Braintrust SDK lifecycle + thin client.

Shared by the v1.x Braintrust triplet (DatasetAdapter / TraceStore /
TraceEnricher). Same hoist pattern as `eval_harness._platforms.langfuse`:
one place that knows about the upstream SDK so auth + connection logic
don't drift across three adapter files.

Braintrust is **not** OTel-native — we don't compose with `OtelClient` the
way the Phoenix / Arize triplets do. Full-fat client. The SDK import is
deferred to construction time and trapped into a `ConfigError` with the
install hint when the `[braintrust]` extra isn't installed. Tests inject a
fake SDK via the keyword-only ``_sdk`` argument and keep the real
`braintrust` package out of the import graph.

Determinism: ``fetch_trace`` polls with caller-injected ``clock`` and
``sleeper`` callbacks so tests can drive ingestion-lag scenarios without
``time.sleep``. Production callers leave both as ``None`` to get the real
``time.monotonic`` + ``asyncio.sleep``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import Callable, Coroutine
from threading import Lock
from typing import Any

from eval_harness.core.errors import ConfigError

# Module-level registry of live clients keyed by (api_key, project, org).
# The same triple shared across all three triplet adapters within one
# `evalh run` returns the same BraintrustClient — exactly one SDK handle,
# one set of connection pools.
_REGISTRY: dict[str, _Entry] = {}
_REGISTRY_LOCK = Lock()


class _Entry:
    __slots__ = ("client", "refcount")

    def __init__(self, client: BraintrustClient) -> None:
        self.client = client
        self.refcount = 0


def _fingerprint(
    api_key: str | None, project: str | None, org: str | None
) -> str:
    return json.dumps(
        {"api_key": api_key, "project": project, "org": org}, sort_keys=True
    )


ClockFn = Callable[[], float]
SleeperFn = Callable[[float], Coroutine[Any, Any, None]]


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class BraintrustClient:
    """Thin facade over the braintrust SDK. Three methods mirror the three
    triplet adapters' needs:

    - ``search_traces(filter)``  → list[dict]    (DatasetAdapter)
    - ``push_trace(payload)``    → None          (TraceStore)
    - ``fetch_trace(trace_id, *, wait_for_ingestion_seconds=0)``
                                 → dict | None   (TraceEnricher)

    Returned dicts are the SDK's own trace / experiment-log shape —
    adapter code does the mapping into our `Trace` / `EvalCase` models.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        project: str | None = None,
        org: str | None = None,
        _sdk: Any | None = None,
        clock: ClockFn | None = None,
        sleeper: SleeperFn | None = None,
    ) -> None:
        self.api_key = api_key
        self.project = project
        self.org = org
        self._clock: ClockFn = clock or time.monotonic
        self._sleep: SleeperFn = sleeper or _real_sleep
        self._shutdown_called = False
        if _sdk is not None:
            self._sdk = _sdk
            return
        self._sdk = _import_and_build_sdk(api_key=api_key, project=project, org=org)

    async def fetch_trace(
        self,
        trace_id: str,
        *,
        wait_for_ingestion_seconds: float = 0.0,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any] | None:
        """Poll Braintrust for a single trace. Ingestion lag is real on
        Braintrust too — the loop uses the injected clock + sleeper so
        tests drive it deterministically."""
        if self._shutdown_called:
            raise RuntimeError("BraintrustClient.fetch_trace called after shutdown")
        deadline = self._clock() + max(wait_for_ingestion_seconds, 0.0)
        while True:
            payload = await _as_async(self._sdk.fetch_trace, trace_id)
            if payload is not None:
                return _to_plain_dict(payload)
            if self._clock() >= deadline:
                return None
            await self._sleep(poll_interval_seconds)

    async def search_traces(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull a (possibly filtered) set of upstream traces. The SDK
        applies the filter server-side."""
        if self._shutdown_called:
            raise RuntimeError("BraintrustClient.search_traces called after shutdown")
        payload = await _as_async(self._sdk.search_traces, filter)
        return [_to_plain_dict(p) for p in payload]

    async def push_trace(self, trace: dict[str, Any]) -> None:
        """Push one trace upward as a Braintrust experiment log entry. The
        SDK decides batching / retries; this client just hands the payload
        through."""
        if self._shutdown_called:
            raise RuntimeError("BraintrustClient.push_trace called after shutdown")
        await _as_async(self._sdk.push_trace, trace)

    async def flush(self) -> None:
        """Best-effort drain of any SDK-side buffer. Called from adapter
        ``__aexit__`` so a TraceStore reliably gets writes out before the
        run's overall AsyncExitStack tears down."""
        flush = getattr(self._sdk, "flush", None)
        if flush is None:
            return
        await _as_async(flush)

    def shutdown(self) -> None:
        """Idempotent shutdown. Releases SDK resources if the underlying
        SDK exposes a ``shutdown``/``close`` method."""
        if self._shutdown_called:
            return
        self._shutdown_called = True
        for name in ("shutdown", "close"):
            fn = getattr(self._sdk, name, None)
            if callable(fn):
                with contextlib.suppress(Exception):  # pragma: no cover — defensive
                    fn()
                return


def _import_and_build_sdk(
    *, api_key: str | None, project: str | None, org: str | None
) -> Any:
    """Import the braintrust SDK and wrap it in a small shim that exposes
    the three methods `BraintrustClient` needs. The shim keeps the rest
    of the codebase free of `braintrust` imports."""
    try:
        import braintrust
    except ImportError as e:
        raise ConfigError(
            "Braintrust platform helper requires the `braintrust` package. "
            "Install with: pip install 'eval-harness[braintrust]'"
        ) from e
    # Production wiring: a logger scoped to the project. The shim handles
    # method-shape translation.
    sdk_logger = braintrust.init_logger(
        project=project,
        api_key=api_key,
        org_name=org,
    )
    return _BraintrustSdkShim(sdk_logger, api_key=api_key, project=project, org=org)


class _BraintrustSdkShim:
    """Production shim: maps `BraintrustClient`'s 3-method API onto the
    Braintrust SDK's surface. Keeps SDK calls out of adapter code."""

    def __init__(
        self,
        sdk: Any,
        *,
        api_key: str | None,
        project: str | None,
        org: str | None,
    ) -> None:
        self._sdk = sdk
        self._api_key = api_key
        self._project = project
        self._org = org

    def fetch_trace(self, trace_id: str) -> Any:
        for name in ("fetch_trace", "get_span", "get_trace"):
            fn = getattr(self._sdk, name, None)
            if fn is not None:
                try:
                    return fn(trace_id)
                except Exception:
                    return None
        raise RuntimeError(
            "braintrust SDK exposes no fetch/get_trace method on the logger"
        )

    def search_traces(self, filter: dict[str, Any]) -> list[Any]:
        for name in ("fetch_traces", "list_spans", "search_traces"):
            fn = getattr(self._sdk, name, None)
            if fn is not None:
                if isinstance(filter, dict):
                    return list(fn(**filter))
                return list(fn(filter))
        raise RuntimeError(
            "braintrust SDK exposes no trace-listing method on the logger"
        )

    def push_trace(self, trace: dict[str, Any]) -> None:
        # Braintrust's logger.log accepts arbitrary kwargs — input, output,
        # metadata, scores. We forward the whole payload so the caller
        # (TraceStore) controls the shape.
        log = getattr(self._sdk, "log", None) or getattr(self._sdk, "create_log", None)
        if log is None:
            raise RuntimeError("braintrust SDK logger exposes no `log` method")
        log(**trace)

    def flush(self) -> None:
        fn = getattr(self._sdk, "flush", None)
        if fn is not None:
            fn()


async def _as_async(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call `fn` and `await` if it returned a coroutine. The Braintrust
    SDK is synchronous; the platform shim still participates in asyncio
    control flow so adapters can be `async def`."""
    out = fn(*args, **kwargs)
    if asyncio.iscoroutine(out):
        return await out
    return out


def _to_plain_dict(payload: Any) -> dict[str, Any]:
    """Convert an SDK return value (pydantic model / dataclass / dict) to a
    plain dict so downstream adapter code doesn't depend on the SDK's
    types."""
    if payload is None:
        return {}
    if isinstance(payload, dict):
        return dict(payload)
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        return dict(model_dump())
    as_dict = getattr(payload, "dict", None)
    if callable(as_dict):
        return dict(as_dict())
    # Last resort: best-effort attribute scrape.
    return {
        k: getattr(payload, k)
        for k in dir(payload)
        if not k.startswith("_") and not callable(getattr(payload, k, None))
    }


def get_or_create_braintrust_client(
    *,
    api_key: str | None,
    project: str | None,
    org: str | None = None,
    _sdk: Any | None = None,
    clock: ClockFn | None = None,
    sleeper: SleeperFn | None = None,
) -> BraintrustClient:
    """Acquire a shared `BraintrustClient` for ``(api_key, project, org)``.
    Bumps the registry refcount; pair every call with
    `release_braintrust_client`."""
    key = _fingerprint(api_key, project, org)
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None:
            client = BraintrustClient(
                api_key=api_key,
                project=project,
                org=org,
                _sdk=_sdk,
                clock=clock,
                sleeper=sleeper,
            )
            entry = _Entry(client=client)
            _REGISTRY[key] = entry
        entry.refcount += 1
        return entry.client


def release_braintrust_client(client: BraintrustClient) -> None:
    """Release a client previously acquired via
    `get_or_create_braintrust_client`. Last release triggers
    `client.shutdown()` and removes the registry entry."""
    key = _fingerprint(client.api_key, client.project, client.org)
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
    "BraintrustClient",
    "get_or_create_braintrust_client",
    "release_braintrust_client",
]
