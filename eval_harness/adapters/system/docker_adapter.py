"""DockerSystemAdapter — start a container, evaluate against it, stop.

Composes with an `inner_adapter` (typically `http`). This adapter owns the
container lifecycle (pull, run, healthcheck, stop) and delegates every
`run(...)` call to a fully-formed inner adapter built from `inner_config`
with `{port}` substituted in.

Mirrors `git_branch_adapter` — the unit of variation is a docker image
instead of a branch.

See docs/Adapters.md > "v1: docker".
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

import httpx

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import EvalCase, RunVariant, Trace

if TYPE_CHECKING:
    from eval_harness.adapters.system.base import SystemAdapter

_HEALTHCHECK_POLL_SECONDS = 0.25


class DockerSystemAdapter:
    name: str

    def __init__(
        self,
        name: str = "docker",
        *,
        image: str | None = None,
        container_port: int | None = None,
        inner_adapter: str | None = None,
        inner_config: dict[str, Any] | None = None,
        healthcheck: str | None = None,
        healthcheck_timeout_seconds: int = 60,
        env: dict[str, str] | None = None,
        volumes: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        try:
            import docker  # noqa: F401
        except ImportError as e:
            raise ConfigError(
                "docker adapter requires the docker SDK. Install with: "
                "pip install 'eval-harness[docker]'"
            ) from e

        if not image:
            raise ConfigError("docker adapter requires 'image'")
        if not inner_adapter:
            raise ConfigError("docker adapter requires 'inner_adapter' (e.g. 'http')")
        if not isinstance(inner_config, dict) or not inner_config:
            raise ConfigError("docker adapter requires 'inner_config' dict")
        if not healthcheck:
            raise ConfigError(
                "docker adapter requires 'healthcheck' (e.g. 'GET /health')"
            )

        self.name = name
        self._image = image
        self._container_port = container_port
        self._inner_adapter_name = inner_adapter
        self._inner_config = dict(inner_config)
        self._healthcheck_method, self._healthcheck_path = _parse_healthcheck(healthcheck)
        self._healthcheck_timeout = float(healthcheck_timeout_seconds)
        self._env = dict(env or {})
        self._volumes = _parse_volumes(volumes or [])
        self._metadata = dict(metadata or {})

        # Runtime state, populated by __aenter__.
        self._port: int | None = None
        self._container: Any = None
        self._inner: SystemAdapter | None = None
        self._exit_stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        import docker

        stack = contextlib.AsyncExitStack()
        try:
            client = docker.from_env()
            await asyncio.to_thread(_ensure_image, client, self._image)

            self._port = _find_free_port()
            container_port = self._container_port or self._port
            self._container = await asyncio.to_thread(
                _run_container,
                client,
                self._image,
                self._port,
                container_port,
                self._env,
                self._volumes,
            )
            stack.push_async_callback(self._cleanup_container)

            await _wait_for_healthcheck(
                self._port,
                self._healthcheck_path,
                self._healthcheck_method,
                self._healthcheck_timeout,
                self._container,
            )

            self._inner = _build_inner_adapter(
                self._inner_adapter_name,
                _render_config(self._inner_config, self._port),
                self.name,
            )
            await stack.enter_async_context(self._inner)
        except BaseException:
            await stack.aclose()
            raise

        self._exit_stack = stack
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None
        self._inner = None
        self._container = None
        self._port = None

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        if self._inner is None:
            raise AdapterError(
                "docker adapter: run() called outside of `async with` context"
            )
        return await self._inner.run(case, variant, workspace)

    async def _cleanup_container(self) -> None:
        container = self._container
        if container is None:
            return
        await asyncio.to_thread(_stop_and_remove, container)


def _ensure_image(client: Any, image: str) -> None:
    """Pull `image` if it's not already present locally."""
    try:
        client.images.get(image)
        return
    except Exception:
        # docker.errors.ImageNotFound — but the docker module is optional,
        # so we accept any "not present" exception and try a pull.
        pass
    try:
        client.images.pull(image)
    except Exception as e:
        raise AdapterError(f"docker adapter: failed to pull image {image!r}: {e}") from e


def _run_container(
    client: Any,
    image: str,
    host_port: int,
    container_port: int,
    env: dict[str, str],
    volumes: dict[str, dict[str, str]],
) -> Any:
    try:
        return client.containers.run(
            image,
            detach=True,
            remove=True,
            ports={f"{container_port}/tcp": host_port},
            environment=env or None,
            volumes=volumes or None,
        )
    except Exception as e:
        raise AdapterError(
            f"docker adapter: failed to start container from image {image!r}: {e}"
        ) from e


def _stop_and_remove(container: Any) -> None:
    with contextlib.suppress(Exception):
        container.stop(timeout=5)
    # `remove=True` at run time handles deletion on stop; explicit remove is a
    # belt-and-braces follow-up if the auto-remove didn't fire.
    with contextlib.suppress(Exception):
        container.remove(force=True)


def _find_free_port() -> int:
    """Bind a transient socket to port 0, read the assigned port, release.

    Same race-window caveat as git_branch — kernel is unlikely to immediately
    re-hand-out the port and docker run will fail loudly if it does.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _parse_healthcheck(value: str) -> tuple[str, str]:
    parts = value.strip().split(maxsplit=1)
    if len(parts) == 1:
        return "GET", parts[0]
    method, path = parts[0].upper(), parts[1]
    if not path.startswith("/"):
        path = "/" + path
    return method, path


def _parse_volumes(specs: list[str]) -> dict[str, dict[str, str]]:
    """Convert `["host:container[:mode]", ...]` to docker SDK volume dict."""
    out: dict[str, dict[str, str]] = {}
    for spec in specs:
        parts = spec.split(":")
        if len(parts) < 2 or len(parts) > 3:
            raise ConfigError(
                f"docker adapter: invalid volume spec {spec!r}; "
                f"expected 'host:container' or 'host:container:mode'"
            )
        host, container = parts[0], parts[1]
        mode = parts[2] if len(parts) == 3 else "rw"
        out[host] = {"bind": container, "mode": mode}
    return out


def _render(template: str, port: int) -> str:
    return template.replace("{port}", str(port))


def _render_config(value: Any, port: int) -> Any:
    if isinstance(value, str):
        return _render(value, port)
    if isinstance(value, list):
        return [_render_config(v, port) for v in value]
    if isinstance(value, dict):
        return {k: _render_config(v, port) for k, v in value.items()}
    return value


async def _wait_for_healthcheck(
    port: int,
    path: str,
    method: str,
    timeout: float,
    container: Any,
) -> None:
    url = f"http://127.0.0.1:{port}{path}"
    deadline = asyncio.get_event_loop().time() + timeout
    last_error: str | None = None
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            status = await asyncio.to_thread(_container_status, container)
            if status not in {None, "created", "running", "restarting"}:
                raise AdapterError(
                    f"docker adapter: container exited (status={status!r}) "
                    f"before healthcheck succeeded"
                )
            try:
                resp = await client.request(method, url)
                if 200 <= resp.status_code < 300:
                    return
                last_error = f"HTTP {resp.status_code}"
            except httpx.HTTPError as e:
                last_error = f"{type(e).__name__}: {e}"
            if asyncio.get_event_loop().time() > deadline:
                raise AdapterError(
                    f"docker adapter: healthcheck {method} {url} did not return "
                    f"2xx within {timeout}s (last: {last_error})"
                )
            await asyncio.sleep(_HEALTHCHECK_POLL_SECONDS)


def _container_status(container: Any) -> str | None:
    with contextlib.suppress(Exception):
        container.reload()
        return str(container.status)
    return None


def _build_inner_adapter(
    inner_adapter: str,
    inner_config: dict[str, Any],
    outer_name: str,
) -> SystemAdapter:
    from eval_harness.factories import system_adapter_factory

    variant = RunVariant(
        name=outer_name,
        adapter=inner_adapter,
        config=inner_config,
    )
    return system_adapter_factory.build(variant)
