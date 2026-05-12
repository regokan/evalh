"""GitBranchSystemAdapter — check out a branch, start the service, evaluate.

Composes with an `inner_adapter` (typically `http`). This adapter does NOT
reimplement HTTP / RPC / process-level concerns — it just owns the
worktree + subprocess + port-discovery + healthcheck lifecycle, then
delegates every `run(...)` call to a fully-formed inner adapter built
from `inner_config` with `{port}` substituted in.

See docs/Adapters.md > "v1: git_branch".
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

import httpx

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import EvalCase, RunVariant, Trace

if TYPE_CHECKING:
    from eval_harness.adapters.system.base import SystemAdapter

_HEALTHCHECK_POLL_SECONDS = 0.25
_KILL_TIMEOUT_SECONDS = 5.0


class GitBranchAdapter:
    name: str

    def __init__(
        self,
        name: str = "git_branch",
        *,
        repo_path: str | None = None,
        branch: str | None = None,
        start_command: list[str] | None = None,
        healthcheck: str | None = None,
        healthcheck_timeout_seconds: int = 30,
        inner_adapter: str | None = None,
        inner_config: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        try:
            import pygit2  # noqa: F401
        except ImportError as e:
            raise ConfigError(
                "git_branch adapter requires pygit2. Install with: "
                "pip install 'eval-harness[git]'"
            ) from e

        if not repo_path:
            raise ConfigError("git_branch adapter requires 'repo_path'")
        if not branch:
            raise ConfigError("git_branch adapter requires 'branch'")
        if not start_command or not isinstance(start_command, list):
            raise ConfigError(
                "git_branch adapter requires 'start_command' (list[str])"
            )
        if not healthcheck:
            raise ConfigError(
                "git_branch adapter requires 'healthcheck' (e.g. 'GET /health')"
            )
        if not inner_adapter:
            raise ConfigError(
                "git_branch adapter requires 'inner_adapter' (e.g. 'http')"
            )
        if not isinstance(inner_config, dict) or not inner_config:
            raise ConfigError(
                "git_branch adapter requires 'inner_config' dict"
            )

        self.name = name
        self._repo_path = Path(repo_path).expanduser().resolve()
        if not self._repo_path.exists():
            raise ConfigError(
                f"git_branch: repo_path does not exist: {self._repo_path}"
            )
        self._branch = branch
        self._start_command = list(start_command)
        self._healthcheck_method, self._healthcheck_path = _parse_healthcheck(healthcheck)
        self._healthcheck_timeout = float(healthcheck_timeout_seconds)
        self._inner_adapter_name = inner_adapter
        self._inner_config = dict(inner_config)
        self._metadata = dict(metadata or {})

        # Runtime state, populated by __aenter__.
        self._port: int | None = None
        self._worktree_path: Path | None = None
        self._worktree_name: str | None = None
        self._process: asyncio.subprocess.Process | None = None
        self._inner: SystemAdapter | None = None
        self._exit_stack: contextlib.AsyncExitStack | None = None

    async def __aenter__(self) -> Self:
        stack = contextlib.AsyncExitStack()
        # If any step fails, unwind everything we'd already done.
        try:
            self._port = _find_free_port()
            self._worktree_name, self._worktree_path = await asyncio.to_thread(
                _add_worktree, self._repo_path, self._branch
            )
            stack.push_async_callback(self._cleanup_worktree)

            rendered_cmd = [_render(arg, self._port) for arg in self._start_command]
            self._process = await asyncio.create_subprocess_exec(
                *rendered_cmd,
                cwd=str(self._worktree_path),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env={**os.environ},
            )
            stack.push_async_callback(self._cleanup_process)

            await _wait_for_healthcheck(
                self._port,
                self._healthcheck_path,
                self._healthcheck_method,
                self._healthcheck_timeout,
                self._process,
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
        self._process = None
        self._worktree_path = None
        self._worktree_name = None
        self._port = None

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        if self._inner is None:
            raise AdapterError(
                "git_branch adapter: run() called outside of `async with` context"
            )
        return await self._inner.run(case, variant, workspace)

    async def _cleanup_process(self) -> None:
        proc = self._process
        if proc is None:
            return
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=_KILL_TIMEOUT_SECONDS)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()

    async def _cleanup_worktree(self) -> None:
        name = self._worktree_name
        path = self._worktree_path
        if path is None or name is None:
            return
        await asyncio.to_thread(_remove_worktree, self._repo_path, name, path)


def _find_free_port() -> int:
    """Bind a transient socket to port 0, read the assigned port, release.

    There is a small race window — another listener could grab the port
    between this function returning and `start_command` actually binding —
    but in practice the kernel won't immediately re-hand out the same port
    and `start_command` will fail loudly if it does.
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


def _add_worktree(repo_path: Path, branch: str) -> tuple[str, Path]:
    import pygit2

    repo = pygit2.Repository(str(repo_path))
    if branch not in repo.branches:
        raise ConfigError(
            f"git_branch: branch '{branch}' not found in {repo_path}; "
            f"known: {sorted(repo.branches.local)}"
        )
    branch_ref = repo.branches[branch]

    parent = Path(tempfile.mkdtemp(prefix="evalh-git-branch-"))
    # pygit2 expects the worktree path to not exist yet.
    worktree_dir = parent / "wt"
    # Use uuid4 for the worktree name: `id(branch_ref)` is the memory
    # address of an ephemeral pygit2 wrapper that can be GC'd + reused
    # between concurrent threads, causing colliding worktree directory
    # names under `.git/worktrees/`. uuid4 is collision-free without
    # threading the registry through callers.
    name = f"evalh-{os.getpid()}-{uuid.uuid4().hex[:12]}"
    wt = repo.add_worktree(name, str(worktree_dir), branch_ref)
    return name, Path(wt.path)


def _remove_worktree(repo_path: Path, name: str, path: Path) -> None:
    import pygit2

    # Tear down the directory first; pygit2.prune is purely metadata.
    parent = path.parent
    shutil.rmtree(path, ignore_errors=True)
    if parent.exists() and parent.name.startswith("evalh-git-branch-"):
        shutil.rmtree(parent, ignore_errors=True)
    repo = pygit2.Repository(str(repo_path))
    with contextlib.suppress(Exception):
        wt = repo.lookup_worktree(name)
        if wt is not None:
            wt.prune(True)


async def _wait_for_healthcheck(
    port: int,
    path: str,
    method: str,
    timeout: float,
    process: asyncio.subprocess.Process,
) -> None:
    url = f"http://127.0.0.1:{port}{path}"
    deadline = asyncio.get_event_loop().time() + timeout
    last_error: str | None = None
    async with httpx.AsyncClient(timeout=2.0) as client:
        while True:
            if process.returncode is not None:
                raise AdapterError(
                    f"git_branch: start_command exited with code "
                    f"{process.returncode} before healthcheck succeeded"
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
                    f"git_branch: healthcheck {method} {url} did not return "
                    f"2xx within {timeout}s (last: {last_error})"
                )
            await asyncio.sleep(_HEALTHCHECK_POLL_SECONDS)


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
