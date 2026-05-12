"""DockerSystemAdapter tests.

Skips cleanly when the docker SDK isn't installed (the [docker] extra) or the
daemon isn't reachable. The composition test uses a fake inner adapter
registered via the singleton factory so we can verify the outer adapter
delegates without actually starting a real service.

Lifecycle tests run against a tiny `python:3-alpine` image started with a
one-liner HTTP server — small, ubiquitous, doesn't require building anything
custom in this bead.
"""

from __future__ import annotations

import shutil
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Any, ClassVar, Self
from unittest.mock import MagicMock

import pytest

pytest.importorskip("docker")

import docker as docker_sdk
from docker.errors import DockerException

from eval_harness.adapters.system.docker_adapter import DockerSystemAdapter
from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import (
    EvalCase,
    RunVariant,
    Trace,
    TraceOutput,
)
from eval_harness.core.time import utc_now
from eval_harness.factories import system_adapter_factory


def _docker_reachable() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        client = docker_sdk.from_env()
        client.ping()
        return True
    except (DockerException, Exception):
        return False


docker_required = pytest.mark.skipif(
    not _docker_reachable(),
    reason="docker daemon not reachable",
)


@contextmanager
def _register_inner(name: str, cls: type[Any]) -> Iterator[None]:
    """Temporarily register an adapter class under `name` in the singleton
    factory, then restore prior state."""
    registry = system_adapter_factory.registry
    prior = registry._items.get(name)
    registry.register(name, cls)
    try:
        yield
    finally:
        if prior is None:
            registry._items.pop(name, None)
        else:
            registry._items[name] = prior


# ------------------------- config / construction --------------------------


def test_missing_image_raises() -> None:
    with pytest.raises(ConfigError, match="image"):
        DockerSystemAdapter(
            inner_adapter="http",
            inner_config={"endpoint": "http://localhost:{port}/x"},
            healthcheck="GET /health",
        )


def test_missing_inner_adapter_raises() -> None:
    with pytest.raises(ConfigError, match="inner_adapter"):
        DockerSystemAdapter(
            image="alpine:3.20",
            inner_config={"endpoint": "http://localhost:{port}/x"},
            healthcheck="GET /health",
        )


def test_missing_inner_config_raises() -> None:
    with pytest.raises(ConfigError, match="inner_config"):
        DockerSystemAdapter(
            image="alpine:3.20",
            inner_adapter="http",
            healthcheck="GET /health",
        )


def test_missing_healthcheck_raises() -> None:
    with pytest.raises(ConfigError, match="healthcheck"):
        DockerSystemAdapter(
            image="alpine:3.20",
            inner_adapter="http",
            inner_config={"endpoint": "http://localhost:{port}/x"},
        )


def test_invalid_volume_spec_raises() -> None:
    with pytest.raises(ConfigError, match="volume"):
        DockerSystemAdapter(
            image="alpine:3.20",
            inner_adapter="http",
            inner_config={"endpoint": "http://localhost:{port}/x"},
            healthcheck="GET /health",
            volumes=["only-one-component"],
        )


def test_factory_registers_docker() -> None:
    from eval_harness.factories import system_adapter_factory

    assert "docker" in system_adapter_factory.registry.names()


def test_parse_volumes_supports_mode() -> None:
    from eval_harness.adapters.system.docker_adapter import _parse_volumes

    parsed = _parse_volumes(["/host/path:/container/path:ro"])
    assert parsed == {"/host/path": {"bind": "/container/path", "mode": "ro"}}


def test_parse_volumes_defaults_to_rw() -> None:
    from eval_harness.adapters.system.docker_adapter import _parse_volumes

    parsed = _parse_volumes(["/host:/container"])
    assert parsed == {"/host": {"bind": "/container", "mode": "rw"}}


def test_parse_healthcheck_defaults_to_get() -> None:
    from eval_harness.adapters.system.docker_adapter import _parse_healthcheck

    assert _parse_healthcheck("/health") == ("GET", "/health")
    assert _parse_healthcheck("GET /health") == ("GET", "/health")
    assert _parse_healthcheck("POST ping") == ("POST", "/ping")


# ------------------------- composition (no real container) ----------------


class _FakeInnerAdapter:
    """Records the (case, variant, config) it was built with and what it's
    asked to do. Provides the minimal SystemAdapter shape."""

    seen_inits: ClassVar[list[dict[str, Any]]] = []
    seen_runs: ClassVar[list[tuple[str, str]]] = []

    def __init__(self, name: str, **config: Any) -> None:
        self.name = name
        self._config = config
        _FakeInnerAdapter.seen_inits.append({"name": name, **config})

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        _FakeInnerAdapter.seen_runs.append((case.id, variant.name))
        now = utc_now()
        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=now,
            finished_at=now,
            latency_ms=0,
            input=case.input,
            output=TraceOutput(final_answer=f"inner-served-{case.id}"),
        )


def _fake_container(status: str = "running") -> Any:
    container = MagicMock()
    container.status = status

    def _reload() -> None:
        container.status = "running"

    container.reload.side_effect = _reload
    container.stop.return_value = None
    container.remove.return_value = None
    return container


def _patch_docker_client(monkeypatch: pytest.MonkeyPatch, container: Any) -> MagicMock:
    """Replace `docker.from_env` to return a fake client that:
       - reports the image is already present
       - hands back `container` on run
       - returns successful 200 on healthcheck via httpx mocking elsewhere
    """
    fake_client = MagicMock()
    fake_client.images.get.return_value = MagicMock()
    fake_client.containers.run.return_value = container
    monkeypatch.setattr("docker.from_env", lambda: fake_client)
    return fake_client


def _patch_healthcheck_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the healthcheck loop succeed on the first poll by short-circuiting
    the wait function entirely."""

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "eval_harness.adapters.system.docker_adapter._wait_for_healthcheck",
        _noop,
    )


async def test_inner_adapter_composition_delegates_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DockerSystemAdapter composes — it must build and delegate to
    inner_adapter rather than reimplementing HTTP. Verified by registering a
    fake adapter and watching what gets built and called."""
    _FakeInnerAdapter.seen_inits = []
    _FakeInnerAdapter.seen_runs = []

    container = _fake_container()
    _patch_docker_client(monkeypatch, container)
    _patch_healthcheck_pass(monkeypatch)

    with _register_inner("fake_inner", _FakeInnerAdapter):
        adapter = DockerSystemAdapter(
            image="alpine:3.20",
            inner_adapter="fake_inner",
            inner_config={
                "endpoint": "http://127.0.0.1:{port}/chat",
                "extra_field": ["a", "b", "port:{port}"],
            },
            healthcheck="GET /health",
        )
        case = EvalCase(id="case_alpha", input={"q": "hi"})
        variant = RunVariant(name="v1", adapter="docker", config={})

        async with adapter:
            port = adapter._port
            assert port is not None
            trace = await adapter.run(case, variant, None)
            await adapter.run(case, variant, None)

    assert len(_FakeInnerAdapter.seen_inits) == 1
    init = _FakeInnerAdapter.seen_inits[0]
    assert init["endpoint"] == f"http://127.0.0.1:{port}/chat"
    assert init["extra_field"] == ["a", "b", f"port:{port}"]
    # The outer adapter's `name` was passed through — no leakage of inner name.
    assert init["name"] == "docker"
    # Two run calls -> two recorded delegations -> none reimplemented in outer.
    assert _FakeInnerAdapter.seen_runs == [
        ("case_alpha", "v1"),
        ("case_alpha", "v1"),
    ]
    assert trace.output.final_answer == "inner-served-case_alpha"

    # Cleanup ran on the container.
    assert container.stop.called or container.remove.called


async def test_image_pull_on_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the image isn't present locally, the adapter pulls it before
    starting the container."""
    container = _fake_container()
    fake_client = MagicMock()

    class _ImageNotFound(Exception):
        pass

    fake_client.images.get.side_effect = _ImageNotFound("not here")
    fake_client.images.pull.return_value = MagicMock()
    fake_client.containers.run.return_value = container
    monkeypatch.setattr("docker.from_env", lambda: fake_client)
    _patch_healthcheck_pass(monkeypatch)

    with _register_inner("fake_inner", _FakeInnerAdapter):
        adapter = DockerSystemAdapter(
            image="ghcr.io/example/agent:v1",
            inner_adapter="fake_inner",
            inner_config={"endpoint": "http://127.0.0.1:{port}"},
            healthcheck="GET /health",
        )
        async with adapter:
            pass

    fake_client.images.pull.assert_called_once_with("ghcr.io/example/agent:v1")


async def test_cleanup_removes_container(monkeypatch: pytest.MonkeyPatch) -> None:
    container = _fake_container()
    _patch_docker_client(monkeypatch, container)
    _patch_healthcheck_pass(monkeypatch)

    with _register_inner("fake_inner", _FakeInnerAdapter):
        adapter = DockerSystemAdapter(
            image="alpine:3.20",
            inner_adapter="fake_inner",
            inner_config={"endpoint": "http://127.0.0.1:{port}"},
            healthcheck="GET /health",
        )
        async with adapter:
            pass

    assert container.stop.called
    # remove() is best-effort follow-up; verify it was attempted.
    assert container.remove.called


async def test_run_outside_context_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    adapter = DockerSystemAdapter(
        image="alpine:3.20",
        inner_adapter="http",
        inner_config={"endpoint": "http://127.0.0.1:{port}"},
        healthcheck="GET /health",
    )
    case = EvalCase(id="c1", input={})
    variant = RunVariant(name="v1", adapter="docker", config={})
    with pytest.raises(AdapterError, match="outside of `async with`"):
        await adapter.run(case, variant, None)


async def test_container_exits_before_healthcheck_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the container dies before the healthcheck passes, the adapter
    surfaces a clear AdapterError instead of looping until timeout."""
    container = MagicMock()
    container.status = "exited"
    container.reload.return_value = None
    container.stop.return_value = None
    container.remove.return_value = None
    _patch_docker_client(monkeypatch, container)

    adapter = DockerSystemAdapter(
        image="alpine:3.20",
        inner_adapter="http",
        inner_config={"endpoint": "http://127.0.0.1:{port}"},
        healthcheck="GET /health",
        healthcheck_timeout_seconds=2,
    )
    with pytest.raises(AdapterError, match="container exited"):
        async with adapter:
            pass


# ------------------------- lifecycle (real docker daemon) -----------------
#
# `nginx:alpine` is small (~25 MB), ubiquitous, and serves an HTML 200 on
# `/` out of the box — no custom command, no rebuild. Container port is the
# nginx default (80); the adapter maps it to a free host port.


@docker_required
@pytest.mark.docker
async def test_lifecycle_pulls_runs_healthchecks_and_stops() -> None:
    """Full lifecycle against a real daemon: pull (or no-op), run, see the
    healthcheck pass, then stop+remove."""
    with _register_inner("fake_inner", _FakeInnerAdapter):
        _FakeInnerAdapter.seen_runs = []
        _FakeInnerAdapter.seen_inits = []
        adapter = DockerSystemAdapter(
            image="nginx:alpine",
            container_port=80,
            inner_adapter="fake_inner",
            inner_config={"endpoint": "http://127.0.0.1:{port}/"},
            healthcheck="GET /",
            healthcheck_timeout_seconds=30,
        )
        case = EvalCase(id="c1", input={})
        variant = RunVariant(name="v1", adapter="docker", config={})
        async with adapter:
            assert adapter._container is not None
            container_id = adapter._container.id
            assert adapter._port is not None
            await adapter.run(case, variant, None)
        # After __aexit__ the container should be removed or at least exited.
        # Docker keeps metadata briefly after stop; both states satisfy
        # "cleanup ran".
        client = docker_sdk.from_env()
        try:
            container = client.containers.get(container_id)
            assert container.status in {"exited", "removing", "dead"}
        except DockerException:
            pass  # NotFound — preferred, container fully removed


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
