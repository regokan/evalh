"""GitBranchAdapter tests.

Skips cleanly when pygit2 isn't installed (the [git] extra). The composition
test uses a fake inner adapter registered via the singleton factory so we can
verify the outer adapter delegates without actually starting a real service.
"""

from __future__ import annotations

import asyncio
import socket
import sys
import textwrap
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any, ClassVar, Self

import pytest

pytest.importorskip("pygit2")

import pygit2

from eval_harness.adapters.system.git_branch_adapter import GitBranchAdapter
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

# ------------------------- shared fixtures --------------------------------


def _init_repo(repo_path: Path, branch: str = "feature") -> str:
    repo = pygit2.init_repository(str(repo_path))
    (repo_path / "app.py").write_text("# stub\n")
    repo.index.add_all()
    repo.index.write()
    sig = pygit2.Signature("test", "test@example.com", 0, 0)
    tree = repo.index.write_tree()
    commit = repo.create_commit("HEAD", sig, sig, "init", tree, [])
    repo.create_branch(branch, repo[commit])
    return str(commit)


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


def test_missing_repo_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="repo_path"):
        GitBranchAdapter(
            branch="feature",
            start_command=["true"],
            healthcheck="GET /health",
            inner_adapter="http",
            inner_config={"endpoint": "http://localhost:{port}/x"},
        )


def test_unknown_repo_path_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="does not exist"):
        GitBranchAdapter(
            repo_path=str(tmp_path / "nope"),
            branch="feature",
            start_command=["true"],
            healthcheck="GET /health",
            inner_adapter="http",
            inner_config={"endpoint": "http://localhost:{port}/x"},
        )


def test_unknown_branch_raises(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir, branch="feature")
    adapter = GitBranchAdapter(
        repo_path=str(repo_dir),
        branch="nonexistent",
        start_command=["true"],
        healthcheck="GET /health",
        inner_adapter="http",
        inner_config={"endpoint": "http://localhost:{port}/x"},
    )

    async def go() -> None:
        async with adapter:
            pass

    with pytest.raises(ConfigError, match="branch 'nonexistent'"):
        asyncio.run(go())


def test_factory_registers_git_branch() -> None:
    assert "git_branch" in system_adapter_factory.registry.names()


# ------------------------- worktree lifecycle -----------------------------


def _python_http_server_cmd(message: str = "ok") -> list[str]:
    """A self-contained Python HTTP server: GET /health -> 200 'ok',
    GET /chat (or anything else) -> 200 with the message."""
    script = textwrap.dedent(
        f"""
        import json
        import sys
        from http.server import BaseHTTPRequestHandler, HTTPServer
        port = int(sys.argv[1])
        class H(BaseHTTPRequestHandler):
            def _send(self, body):
                data = body.encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def do_GET(self):
                if self.path == "/health":
                    self._send('{{"ok": true}}')
                else:
                    self._send(json.dumps({{"answer": "{message}"}}))
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self._send(json.dumps({{"answer": "{message}"}}))
            def log_message(self, *args): pass
        HTTPServer(("127.0.0.1", port), H).serve_forever()
        """
    )
    return [sys.executable, "-c", script, "{port}"]


async def test_worktree_added_for_branch_and_cleaned_up(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir, branch="feature")

    adapter = GitBranchAdapter(
        repo_path=str(repo_dir),
        branch="feature",
        start_command=_python_http_server_cmd(),
        healthcheck="GET /health",
        healthcheck_timeout_seconds=10,
        inner_adapter="http",
        inner_config={
            "endpoint": "http://127.0.0.1:{port}/chat",
            "response_mapping": {"final_answer": "$.answer"},
        },
    )

    async with adapter:
        assert adapter._worktree_path is not None
        wt_path = adapter._worktree_path
        assert wt_path.is_dir()
        # Branch checkout: app.py committed on `feature` should be present.
        assert (wt_path / "app.py").is_file()
        # Worktree is registered with the parent repo.
        repo = pygit2.Repository(str(repo_dir))
        assert adapter._worktree_name in repo.list_worktrees()
        # Resolved port is sensible (>= 1024 typically; just assert a port set).
        assert adapter._port is not None and adapter._port > 0

    # After exit: worktree dir gone, repo metadata pruned.
    assert not wt_path.exists()
    repo = pygit2.Repository(str(repo_dir))
    assert adapter._worktree_name not in repo.list_worktrees()


async def test_starts_inner_http_adapter_with_resolved_port(tmp_path: Path) -> None:
    """The {port} substitution flows from socket-discovery into both the
    start_command AND the inner_config's endpoint URL."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir, branch="feature")

    adapter = GitBranchAdapter(
        repo_path=str(repo_dir),
        branch="feature",
        start_command=_python_http_server_cmd(message="hello"),
        healthcheck="GET /health",
        healthcheck_timeout_seconds=10,
        inner_adapter="http",
        inner_config={
            "endpoint": "http://127.0.0.1:{port}/chat",
            "response_mapping": {"final_answer": "$.answer"},
        },
    )
    case = EvalCase(id="c1", input={"user_message": "hi"})
    variant = RunVariant(name="v1", adapter="git_branch", config={})

    async with adapter:
        trace = await adapter.run(case, variant, None)

    assert isinstance(trace, Trace)
    assert trace.output.final_answer == "hello"


async def test_cleanup_kills_process_on_exit(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir, branch="feature")

    adapter = GitBranchAdapter(
        repo_path=str(repo_dir),
        branch="feature",
        start_command=_python_http_server_cmd(),
        healthcheck="GET /health",
        healthcheck_timeout_seconds=10,
        inner_adapter="http",
        inner_config={
            "endpoint": "http://127.0.0.1:{port}/chat",
            "response_mapping": {"final_answer": "$.answer"},
        },
    )
    async with adapter:
        proc = adapter._process
        assert proc is not None
        assert proc.returncode is None

    # Process should have terminated.
    assert proc is not None
    assert proc.returncode is not None


async def test_healthcheck_timeout_when_service_never_listens(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir, branch="feature")

    # `sleep 30` never opens a port.
    adapter = GitBranchAdapter(
        repo_path=str(repo_dir),
        branch="feature",
        start_command=["sleep", "30"],
        healthcheck="GET /health",
        healthcheck_timeout_seconds=1,
        inner_adapter="http",
        inner_config={
            "endpoint": "http://127.0.0.1:{port}/x",
            "response_mapping": {"final_answer": "$.x"},
        },
    )

    async def go() -> None:
        async with adapter:
            pass

    with pytest.raises(AdapterError, match="healthcheck"):
        await go()


async def test_run_outside_context_raises(tmp_path: Path) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir, branch="feature")

    adapter = GitBranchAdapter(
        repo_path=str(repo_dir),
        branch="feature",
        start_command=["true"],
        healthcheck="GET /health",
        inner_adapter="http",
        inner_config={
            "endpoint": "http://127.0.0.1:{port}/x",
            "response_mapping": {"final_answer": "$.x"},
        },
    )
    case = EvalCase(id="c1", input={})
    variant = RunVariant(name="v1", adapter="git_branch", config={})
    with pytest.raises(AdapterError, match="outside of `async with`"):
        await adapter.run(case, variant, None)


# ------------------------- composition ------------------------------------


class _FakeInnerAdapter:
    """Records the (case, variant, config) it was built with and what it's
    asked to do. Provides the minimal SystemAdapter shape: __aenter__,
    __aexit__, run."""

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


async def test_inner_adapter_composition_delegates_run(tmp_path: Path) -> None:
    """GitBranchAdapter composes — it must build and delegate to inner_adapter
    rather than reimplementing HTTP. Verified by registering a fake adapter
    and watching what gets built and called."""
    _FakeInnerAdapter.seen_inits = []
    _FakeInnerAdapter.seen_runs = []

    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir, branch="feature")

    with _register_inner("fake_inner", _FakeInnerAdapter):
        adapter = GitBranchAdapter(
            repo_path=str(repo_dir),
            branch="feature",
            start_command=_python_http_server_cmd(),
            healthcheck="GET /health",
            healthcheck_timeout_seconds=10,
            inner_adapter="fake_inner",
            inner_config={
                "endpoint": "http://127.0.0.1:{port}/chat",
                "extra_field": ["a", "b", "port:{port}"],
            },
        )
        case = EvalCase(id="case_alpha", input={"q": "hi"})
        variant = RunVariant(name="v1", adapter="git_branch", config={})

        async with adapter:
            port = adapter._port
            assert port is not None
            trace = await adapter.run(case, variant, None)
            await adapter.run(case, variant, None)

    # Inner adapter was built exactly once with the substituted port and
    # carried through extra_field substitutions (proving recursive render).
    assert len(_FakeInnerAdapter.seen_inits) == 1
    init = _FakeInnerAdapter.seen_inits[0]
    assert init["endpoint"] == f"http://127.0.0.1:{port}/chat"
    assert init["extra_field"] == ["a", "b", f"port:{port}"]
    # The outer adapter's `name` was passed through to the inner — composition,
    # not adapter-name leaking to the trace.
    assert init["name"] == "git_branch"
    # Two run calls -> two recorded delegations -> none reimplemented in outer.
    assert _FakeInnerAdapter.seen_runs == [
        ("case_alpha", "v1"),
        ("case_alpha", "v1"),
    ]
    assert trace.output.final_answer == "inner-served-case_alpha"


# ------------------------- security ---------------------------------------


def test_start_command_is_list_not_shell_string(tmp_path: Path) -> None:
    """The Protocol commits to `list[str]` so callers can't accidentally pass a
    shell string that would land in subprocess shell=True territory."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    _init_repo(repo_dir, branch="feature")
    with pytest.raises(ConfigError, match="list"):
        GitBranchAdapter(
            repo_path=str(repo_dir),
            branch="feature",
            start_command="bash -c 'evil shell string'",  # type: ignore[arg-type]
            healthcheck="GET /health",
            inner_adapter="http",
            inner_config={"endpoint": "http://localhost:{port}/x"},
        )


# ------------------------- helpers (unit-level) ---------------------------


def test_find_free_port_returns_unused_port() -> None:
    from eval_harness.adapters.system.git_branch_adapter import _find_free_port

    port = _find_free_port()
    assert 1024 <= port <= 65535
    # Port is actually free at the moment we return — bind sanity check.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_parse_healthcheck_defaults_to_get() -> None:
    from eval_harness.adapters.system.git_branch_adapter import _parse_healthcheck

    assert _parse_healthcheck("/health") == ("GET", "/health")
    assert _parse_healthcheck("GET /health") == ("GET", "/health")
    assert _parse_healthcheck("POST /up") == ("POST", "/up")
