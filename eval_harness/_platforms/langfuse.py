"""Langfuse SDK lifecycle + thin client.

Shared by the v1-supplement Langfuse triplet (DatasetAdapter / TraceStore /
TraceEnricher). Same hoist pattern as `eval_harness.core.llm_backends` and
`eval_harness._platforms.otel`: one place that knows about the upstream SDK
so auth and connection logic don't drift across three adapter files.

The SDK import is deferred to construction time and trapped into a
`ConfigError` with the install hint when the `[langfuse]` extra isn't
installed. Tests inject a fake SDK via the keyword-only ``_sdk`` argument and
keep the real `langfuse` package out of the import graph.

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
from typing import TYPE_CHECKING, Any

from eval_harness.core.errors import ConfigError

if TYPE_CHECKING:
    pass

# Module-level registry of live clients, keyed by (host, api_key). The same
# (host, api_key) pair shared across all three triplet adapters within one
# `evalh run` returns the same LangfuseClient instance — exactly one SDK
# handle, one set of connection pools.
_REGISTRY: dict[str, _Entry] = {}
_REGISTRY_LOCK = Lock()


class _Entry:
    __slots__ = ("client", "refcount")

    def __init__(self, client: LangfuseClient) -> None:
        self.client = client
        self.refcount = 0


def _fingerprint(api_key: str | None, host: str | None) -> str:
    return json.dumps({"api_key": api_key, "host": host}, sort_keys=True)


# Clock callback returns a monotonic float (seconds). Sleeper is `await`ed
# with the number of seconds to sleep. Both are injectable for tests.
ClockFn = Callable[[], float]
SleeperFn = Callable[[float], Coroutine[Any, Any, None]]


async def _real_sleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


class LangfuseClient:
    """Thin facade over the langfuse SDK. Three methods, mirroring the three
    triplet adapters' needs:

    - ``search_traces(filter)``  → list[dict]    (DatasetAdapter)
    - ``push_trace(payload)``    → None          (TraceStore)
    - ``fetch_trace(trace_id, *, wait_for_ingestion_seconds=0)``
                                 → dict | None   (TraceEnricher)

    Returned dicts are the SDK's own trace shape — adapter code maps them
    into our `Trace` / `EvalCase` models. That mapping is the adapter's job,
    not this client's.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        host: str | None = None,
        _sdk: Any | None = None,
        clock: ClockFn | None = None,
        sleeper: SleeperFn | None = None,
    ) -> None:
        self.api_key = api_key
        self.host = host
        self._clock: ClockFn = clock or time.monotonic
        self._sleep: SleeperFn = sleeper or _real_sleep
        self._shutdown_called = False
        if _sdk is not None:
            self._sdk = _sdk
            return
        self._sdk = _import_and_build_sdk(api_key=api_key, host=host)

    async def fetch_trace(
        self,
        trace_id: str,
        *,
        wait_for_ingestion_seconds: float = 0.0,
        poll_interval_seconds: float = 0.5,
    ) -> dict[str, Any] | None:
        """Fetch a single upstream trace by id.

        When ``wait_for_ingestion_seconds`` > 0 we poll up to that bound
        because Langfuse buffers writes before they become readable. The
        loop uses the injected clock + sleeper so tests drive it
        deterministically.
        """
        if self._shutdown_called:
            raise RuntimeError("LangfuseClient.fetch_trace called after shutdown")
        deadline = self._clock() + wait_for_ingestion_seconds
        attempts = 0
        while True:
            attempts += 1
            payload = await _as_async(self._sdk.fetch_trace, trace_id)
            if payload is not None:
                return _to_plain_dict(payload)
            if self._clock() >= deadline:
                return None
            await self._sleep(poll_interval_seconds)

    async def search_traces(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        """Pull a (possibly filtered) page of upstream traces.

        Forwarded straight to the SDK; the SDK is expected to apply the
        filter on its side. ``sample`` semantics belong to the caller.
        """
        if self._shutdown_called:
            raise RuntimeError("LangfuseClient.search_traces called after shutdown")
        payload = await _as_async(self._sdk.search_traces, filter)
        return [_to_plain_dict(p) for p in payload]

    async def push_trace(self, trace: dict[str, Any]) -> None:
        """Push one trace upward. The SDK decides batching / retries; this
        client just hands the payload through."""
        if self._shutdown_called:
            raise RuntimeError("LangfuseClient.push_trace called after shutdown")
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
        """Idempotent shutdown. Releases SDK resources if the underlying SDK
        exposes a ``shutdown``/``close`` method."""
        if self._shutdown_called:
            return
        self._shutdown_called = True
        for name in ("shutdown", "close"):
            fn = getattr(self._sdk, name, None)
            if callable(fn):
                with contextlib.suppress(Exception):  # pragma: no cover — defensive
                    fn()
                return


def _import_and_build_sdk(*, api_key: str | None, host: str | None) -> Any:
    """Import the langfuse SDK and wrap it in a small adapter object that
    exposes the three methods `LangfuseClient` needs. The wrapper keeps the
    rest of the codebase free of `langfuse` imports.
    """
    try:
        import langfuse
    except ImportError as e:
        raise ConfigError(
            "Langfuse platform helper requires the `langfuse` package. "
            "Install with: pip install 'eval-harness[langfuse]'"
        ) from e
    sdk = langfuse.Langfuse(secret_key=api_key, public_key=api_key, host=host)
    return _LangfuseSdkShim(sdk)


class _LangfuseSdkShim:
    """Production shim: maps `LangfuseClient`'s 3-method API onto the SDK's
    actual surface. Keeps SDK calls out of adapter code.
    """

    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk

    def fetch_trace(self, trace_id: str) -> Any:
        get = getattr(self._sdk, "fetch_trace", None) or getattr(
            self._sdk, "get_trace", None
        )
        if get is None:
            raise RuntimeError(
                "langfuse SDK exposes neither `fetch_trace` nor `get_trace`"
            )
        try:
            return get(trace_id)
        except Exception:
            return None

    def search_traces(self, filter: dict[str, Any]) -> list[Any]:
        for name in ("fetch_traces", "get_traces", "search_traces"):
            fn = getattr(self._sdk, name, None)
            if fn is not None:
                return list(fn(**filter) if isinstance(filter, dict) else fn(filter))
        raise RuntimeError(
            "langfuse SDK exposes no trace-listing method; tried "
            "fetch_traces/get_traces/search_traces"
        )

    def push_trace(self, trace: dict[str, Any]) -> None:
        for name in ("trace", "create_trace"):
            fn = getattr(self._sdk, name, None)
            if fn is not None:
                fn(**trace) if isinstance(trace, dict) else fn(trace)
                return
        raise RuntimeError("langfuse SDK exposes no trace-create method")

    def flush(self) -> None:
        fn = getattr(self._sdk, "flush", None)
        if fn is not None:
            fn()


async def _as_async(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Call `fn` and `await` if it returned a coroutine/awaitable. The
    Langfuse SDK is synchronous; the platform shim still has to participate
    in asyncio control flow so adapters can be `async def`."""
    out = fn(*args, **kwargs)
    if asyncio.iscoroutine(out):
        return await out
    return out


def _to_plain_dict(payload: Any) -> dict[str, Any]:
    """Convert an SDK return value (pydantic model / dataclass / dict) to a
    plain dict so downstream adapter code doesn't depend on the SDK's types."""
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


def get_or_create_langfuse_client(
    *,
    api_key: str | None,
    host: str | None,
    _sdk: Any | None = None,
    clock: ClockFn | None = None,
    sleeper: SleeperFn | None = None,
) -> LangfuseClient:
    """Acquire a shared `LangfuseClient` for ``(host, api_key)``. Bumps the
    registry refcount; pair every call with `release_langfuse_client`."""
    key = _fingerprint(api_key, host)
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None:
            client = LangfuseClient(
                api_key=api_key,
                host=host,
                _sdk=_sdk,
                clock=clock,
                sleeper=sleeper,
            )
            entry = _Entry(client=client)
            _REGISTRY[key] = entry
        entry.refcount += 1
        return entry.client


def release_langfuse_client(client: LangfuseClient) -> None:
    """Release a client previously acquired via `get_or_create_langfuse_client`.
    Last release triggers `client.shutdown()` and removes the registry entry."""
    key = _fingerprint(client.api_key, client.host)
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None or entry.client is not client:
            return
        entry.refcount -= 1
        if entry.refcount <= 0:
            _REGISTRY.pop(key, None)
            client.shutdown()


def _clear_registry_for_tests() -> None:
    """Test helper: nuke the registry. Tests that exercise refcount paths
    should pair their own acquires + releases instead of calling this."""
    with _REGISTRY_LOCK:
        entries = list(_REGISTRY.values())
        _REGISTRY.clear()
    for e in entries:
        e.client.shutdown()


def _registry_snapshot() -> dict[str, int]:
    """Test helper: peek at refcounts without exposing live entries."""
    with _REGISTRY_LOCK:
        return {k: v.refcount for k, v in _REGISTRY.items()}


__all__ = [
    "LangfuseClient",
    "get_or_create_langfuse_client",
    "release_langfuse_client",
]
