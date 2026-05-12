"""OpenTelemetry SDK lifecycle helper.

`OtelClient` owns one `TracerProvider` + one OTLP exporter. The TracerProvider
is constructed on the FIRST `get_tracer_provider()` call (lazy) and torn down
in `shutdown()` (called from the consumer's `__aexit__`). Multiple OTel-shaped
adapters in the same run that target the same endpoint SHARE a TracerProvider
via `get_or_create_otel_client` — exactly one set of spans + resource
attributes lands in the backend, grouped naturally.

Per-run lifecycle: the registry is reference-counted. The TracerProvider is
shut down when the last consumer releases the client. This keeps the lifecycle
scoped to a single `evalh run` (each run's AsyncExitStack acquires + releases
deterministically) without leaking SDK state across runs.

[otel] extra required:
    pip install 'eval-harness[otel]'
"""

from __future__ import annotations

import contextlib
import json
from dataclasses import dataclass, field
from threading import Lock
from typing import TYPE_CHECKING, Any, cast

from eval_harness.core.errors import ConfigError

if TYPE_CHECKING:
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.trace import Tracer

# Module-level registry of live clients keyed by config fingerprint. Multiple
# acquire(...) calls with the same key return the same client and bump a
# refcount; release(...) decrements and shuts down at zero.
_REGISTRY: dict[str, _Entry] = {}
_REGISTRY_LOCK = Lock()


@dataclass
class _Entry:
    client: OtelClient
    refcount: int = 0


@dataclass
class _OtelDeps:
    """Lazy import bundle. None until `_import_otel_deps` is called."""

    Resource: Any = None
    TracerProvider: Any = None
    BatchSpanProcessor: Any = None
    OTLPHttpExporter: Any = None
    OTLPGrpcExporter: Any = None
    trace_api: Any = None
    field_init: bool = field(default=False, init=False)


def _import_otel_deps() -> _OtelDeps:
    """Import the OTel SDK + OTLP exporters; raise `ConfigError` with an
    install hint if the [otel] extra isn't installed."""
    try:
        from opentelemetry import trace as trace_api
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as OTLPHttpExporter,
        )
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as e:
        raise ConfigError(
            "OTel platform helper requires the `opentelemetry-sdk` + "
            "`opentelemetry-exporter-otlp` packages. Install with: "
            "pip install 'eval-harness[otel]'"
        ) from e
    # gRPC exporter is optional — protocol='grpc' callers need it; http
    # callers don't. Probe lazily so missing grpcio doesn't break http users.
    grpc_exporter: Any = None
    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
            OTLPSpanExporter as _OTLPGrpcExporter,
        )

        grpc_exporter = _OTLPGrpcExporter
    except ImportError:
        grpc_exporter = None
    return _OtelDeps(
        Resource=Resource,
        TracerProvider=TracerProvider,
        BatchSpanProcessor=BatchSpanProcessor,
        OTLPHttpExporter=OTLPHttpExporter,
        OTLPGrpcExporter=grpc_exporter,
        trace_api=trace_api,
    )


def _fingerprint(
    endpoint: str,
    headers: dict[str, str] | None,
    protocol: str,
    resource_attributes: dict[str, str] | None,
) -> str:
    """Stable hash-equivalent key for the registry. Headers / resource attrs
    sorted so caller key order doesn't matter."""
    return json.dumps(
        {
            "endpoint": endpoint,
            "headers": dict(sorted((headers or {}).items())),
            "protocol": protocol,
            "resource_attributes": dict(sorted((resource_attributes or {}).items())),
        },
        sort_keys=True,
    )


class OtelClient:
    """Owns one `TracerProvider` + one OTLP exporter for one (endpoint,
    headers, protocol, resource_attributes) target.

    Construct directly for one-shot / test scenarios. Production callers
    should use `get_or_create_otel_client` so multiple adapters share state.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        headers: dict[str, str] | None = None,
        protocol: str = "http",
        resource_attributes: dict[str, str] | None = None,
    ) -> None:
        if protocol not in {"http", "grpc"}:
            raise ConfigError(
                f"OtelClient: protocol must be 'http' or 'grpc'; got {protocol!r}"
            )
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.protocol = protocol
        self.resource_attributes = dict(resource_attributes or {})
        self._deps = _import_otel_deps()
        self._provider: TracerProvider | None = None
        self._shutdown_called = False

    def get_tracer_provider(self) -> TracerProvider:
        """Lazy-construct the TracerProvider on first call. Subsequent calls
        return the same instance."""
        if self._provider is not None:
            return self._provider
        if self._shutdown_called:
            raise RuntimeError(
                "OtelClient.get_tracer_provider called after shutdown"
            )
        exporter_cls = (
            self._deps.OTLPHttpExporter
            if self.protocol == "http"
            else self._deps.OTLPGrpcExporter
        )
        if exporter_cls is None:
            raise ConfigError(
                "OtelClient: protocol='grpc' selected but the grpc exporter "
                "isn't installed. Install with: pip install "
                "'opentelemetry-exporter-otlp-proto-grpc'"
            )
        from opentelemetry.sdk.trace import TracerProvider as _TracerProvider

        exporter = exporter_cls(endpoint=self.endpoint, headers=self.headers)
        resource = self._deps.Resource.create(self.resource_attributes)
        provider: Any = self._deps.TracerProvider(resource=resource)
        provider.add_span_processor(self._deps.BatchSpanProcessor(exporter))
        # `cast` is honoured under both env profiles: with the [otel] extra
        # installed mypy narrows the `Any` to the real `TracerProvider`;
        # without the extra, `ignore_missing_imports=true` makes the cast
        # target itself `Any`, so the cast is a no-op rather than an
        # unused-ignore violation.
        typed = cast(_TracerProvider, provider)
        self._provider = typed
        return typed

    def get_tracer(self, name: str) -> Tracer:
        """Convenience: return a tracer bound to this client's provider."""
        provider = self.get_tracer_provider()
        return provider.get_tracer(name)

    def shutdown(self) -> None:
        """Flush queued spans + close the exporter. Idempotent."""
        if self._shutdown_called:
            return
        self._shutdown_called = True
        provider = self._provider
        self._provider = None
        if provider is None:
            return
        # `shutdown()` on the SDK provider flushes the BatchSpanProcessor
        # and closes the underlying exporter.
        provider.shutdown()


def get_or_create_otel_client(
    *,
    endpoint: str,
    headers: dict[str, str] | None = None,
    protocol: str = "http",
    resource_attributes: dict[str, str] | None = None,
) -> OtelClient:
    """Acquire a shared OtelClient for `(endpoint, headers, protocol,
    resource_attributes)`. Bumps the registry refcount; pair every call with
    `release_otel_client(client)`.

    Two callers with identical config get the same `OtelClient` instance
    (and therefore the same `TracerProvider` + exporter) — that's the
    sharing invariant the v1-supplement OTel triplet depends on."""
    key = _fingerprint(endpoint, headers, protocol, resource_attributes)
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None:
            client = OtelClient(
                endpoint=endpoint,
                headers=headers,
                protocol=protocol,
                resource_attributes=resource_attributes,
            )
            entry = _Entry(client=client, refcount=0)
            _REGISTRY[key] = entry
        entry.refcount += 1
        return entry.client


def release_otel_client(client: OtelClient) -> None:
    """Release a client previously acquired via `get_or_create_otel_client`.
    The last release triggers `client.shutdown()` and removes the registry
    entry so the next acquire builds a fresh client (per-run lifecycle)."""
    key = _fingerprint(
        client.endpoint,
        client.headers,
        client.protocol,
        client.resource_attributes,
    )
    with _REGISTRY_LOCK:
        entry = _REGISTRY.get(key)
        if entry is None or entry.client is not client:
            # Caller released something we don't own; ignore — defensive.
            return
        entry.refcount -= 1
        if entry.refcount <= 0:
            _REGISTRY.pop(key, None)
            client.shutdown()


def _registry_snapshot() -> dict[str, int]:
    """Test helper: peek at the registry without exposing live entries."""
    with _REGISTRY_LOCK:
        return {k: v.refcount for k, v in _REGISTRY.items()}


def _clear_registry_for_tests() -> None:
    """Test helper: nuke the registry and shut down every live client.
    Called from tests that need a clean slate without depending on prior
    test acquire/release pairing."""
    with _REGISTRY_LOCK:
        entries = list(_REGISTRY.values())
        _REGISTRY.clear()
    for entry in entries:
        with contextlib.suppress(Exception):
            entry.client.shutdown()


__all__ = [
    "OtelClient",
    "get_or_create_otel_client",
    "release_otel_client",
]
