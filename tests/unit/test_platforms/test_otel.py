"""OtelClient + shared-singleton tests.

These tests don't actually export to a real OTel collector — they verify
the helper constructs the SDK objects correctly, that two callers with
the same fingerprint share a TracerProvider, and that the registry
refcount drives shutdown.

Skips cleanly when the [otel] extra isn't installed.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

pytest.importorskip("opentelemetry.sdk.trace")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")

from eval_harness._platforms import otel as otel_mod
from eval_harness._platforms.otel import (
    OtelClient,
    get_or_create_otel_client,
    release_otel_client,
)
from eval_harness.core.errors import ConfigError


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    """Every test starts with an empty registry — prior failures shouldn't
    leak live providers."""
    otel_mod._clear_registry_for_tests()
    try:
        yield None
    finally:
        otel_mod._clear_registry_for_tests()


# ---- Construction --------------------------------------------------------


def test_otel_client_constructs_tracer_provider() -> None:
    """Calling get_tracer_provider lazily instantiates the SDK provider."""
    from opentelemetry.sdk.trace import TracerProvider

    client = OtelClient(
        endpoint="http://localhost:4318/v1/traces",
        headers={"x-api-key": "abc"},
        resource_attributes={"service.name": "evalh"},
    )
    provider = client.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    # Second call returns the same instance (idempotent).
    assert client.get_tracer_provider() is provider


def test_otel_client_get_tracer_returns_bound_tracer() -> None:
    client = OtelClient(endpoint="http://localhost:4318/v1/traces")
    tracer = client.get_tracer("eval_harness.test")
    assert tracer is not None
    # Tracers have a .start_span method — minimal protocol smoke check.
    assert hasattr(tracer, "start_as_current_span")


def test_otel_client_rejects_unknown_protocol() -> None:
    with pytest.raises(ConfigError, match="protocol"):
        OtelClient(endpoint="http://x", protocol="unknown")


# ---- Shutdown ------------------------------------------------------------


def test_otel_client_shutdown_flushes_provider() -> None:
    """shutdown() delegates to TracerProvider.shutdown() (which flushes the
    BatchSpanProcessor + closes the exporter). We verify the call via the
    spy below; if the SDK API ever rearranges, this test will fail visibly."""
    client = OtelClient(endpoint="http://localhost:4318/v1/traces")
    provider = client.get_tracer_provider()
    called: dict[str, bool] = {"shutdown": False}
    orig_shutdown = provider.shutdown

    def fake_shutdown() -> Any:
        called["shutdown"] = True
        return orig_shutdown()

    provider.shutdown = fake_shutdown  # type: ignore[method-assign]
    client.shutdown()
    assert called["shutdown"] is True


def test_otel_client_shutdown_is_idempotent() -> None:
    client = OtelClient(endpoint="http://localhost:4318/v1/traces")
    client.get_tracer_provider()
    client.shutdown()
    # Second shutdown is a no-op, not an error.
    client.shutdown()


def test_otel_client_get_tracer_provider_after_shutdown_raises() -> None:
    client = OtelClient(endpoint="http://localhost:4318/v1/traces")
    client.get_tracer_provider()
    client.shutdown()
    with pytest.raises(RuntimeError, match="after shutdown"):
        client.get_tracer_provider()


def test_otel_client_shutdown_before_use_is_safe() -> None:
    client = OtelClient(endpoint="http://localhost:4318/v1/traces")
    client.shutdown()  # never called get_tracer_provider — should not crash


# ---- Shared singleton ----------------------------------------------------


def test_two_adapters_same_endpoint_share_tracer_provider() -> None:
    """The headline sharing invariant: two callers with the same fingerprint
    get the same `OtelClient` (and therefore the same TracerProvider). This
    is what lets `OtelTraceStore` and `OtelTraceEnricher` co-exist in the
    same run without duplicating spans."""
    a = get_or_create_otel_client(
        endpoint="http://localhost:4318/v1/traces",
        headers={"x-api-key": "abc"},
        resource_attributes={"service.name": "evalh"},
    )
    b = get_or_create_otel_client(
        endpoint="http://localhost:4318/v1/traces",
        headers={"x-api-key": "abc"},
        resource_attributes={"service.name": "evalh"},
    )
    assert a is b
    assert a.get_tracer_provider() is b.get_tracer_provider()


def test_different_endpoints_get_distinct_clients() -> None:
    a = get_or_create_otel_client(endpoint="http://a:4318/v1/traces")
    b = get_or_create_otel_client(endpoint="http://b:4318/v1/traces")
    assert a is not b


def test_different_headers_get_distinct_clients() -> None:
    a = get_or_create_otel_client(
        endpoint="http://localhost:4318/v1/traces", headers={"k": "1"}
    )
    b = get_or_create_otel_client(
        endpoint="http://localhost:4318/v1/traces", headers={"k": "2"}
    )
    assert a is not b


def test_fingerprint_is_header_order_insensitive() -> None:
    """Header dict ordering must not split the singleton."""
    a = get_or_create_otel_client(
        endpoint="http://localhost:4318/v1/traces",
        headers={"x-one": "1", "x-two": "2"},
    )
    b = get_or_create_otel_client(
        endpoint="http://localhost:4318/v1/traces",
        headers={"x-two": "2", "x-one": "1"},
    )
    assert a is b


def test_refcount_drives_shutdown() -> None:
    """release_otel_client is refcounted: the last release calls
    `client.shutdown()` and frees the registry entry."""
    a = get_or_create_otel_client(endpoint="http://localhost:4318/v1/traces")
    b = get_or_create_otel_client(endpoint="http://localhost:4318/v1/traces")
    assert a is b
    snapshot = otel_mod._registry_snapshot()
    assert sum(snapshot.values()) == 2

    release_otel_client(a)
    # Still alive — second consumer holds a ref.
    assert sum(otel_mod._registry_snapshot().values()) == 1
    assert not a._shutdown_called

    release_otel_client(b)
    # All refs released -> entry gone, client shut down.
    assert otel_mod._registry_snapshot() == {}
    assert a._shutdown_called


def test_release_after_full_release_is_a_noop() -> None:
    """Defensive: an extra release shouldn't blow up if the caller miscounted."""
    a = get_or_create_otel_client(endpoint="http://localhost:4318/v1/traces")
    release_otel_client(a)
    release_otel_client(a)  # no-op; registry already empty


def test_acquire_after_full_release_builds_fresh_client() -> None:
    """Per-run lifecycle: a new evalh run that re-acquires the same endpoint
    after the previous run released must get a fresh `OtelClient`."""
    a = get_or_create_otel_client(endpoint="http://localhost:4318/v1/traces")
    release_otel_client(a)
    b = get_or_create_otel_client(endpoint="http://localhost:4318/v1/traces")
    assert b is not a


# ---- Missing SDK ---------------------------------------------------------


def test_import_helper_raises_configerror_when_sdk_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate the [otel] extra being uninstalled; the helper must raise
    `ConfigError` with an install hint instead of leaking the ImportError."""
    import builtins
    import importlib

    real_import = builtins.__import__

    def fake_import(
        name: str,
        globals: Any = None,
        locals: Any = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        # Match either the top-level `opentelemetry` import or any submodule
        # so the try/except in _import_otel_deps trips on the very first
        # SDK access.
        if name == "opentelemetry" or name.startswith("opentelemetry."):
            raise ImportError(f"simulated missing {name}")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    mod = importlib.reload(importlib.import_module("eval_harness._platforms.otel"))
    with pytest.raises(ConfigError) as exc:
        mod.OtelClient(endpoint="http://localhost:4318/v1/traces")
    assert "eval-harness[otel]" in str(exc.value)
