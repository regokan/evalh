"""Roadmap v1 done-when: coding-agent eval against three branches in parallel
produces per-branch test-pass deltas.

We don't shell out to a real LLM here — that's the smoke test under
`examples/coding_agent/`. This is the *infrastructure* invariant: three
git_branch SystemAdapters dispatch concurrently against three branches of
one repo, each branch boots its own HTTP service from the worktree, and
the runner's per-variant rollup correctly shows pass/fail deltas across
the three.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("pygit2")

import pygit2

from eval_harness.adapters.system.git_branch_adapter import GitBranchAdapter
from eval_harness.core.models import EvalCase, RunVariant
from eval_harness.core.time import utc_now


def _commit(repo: pygit2.Repository, sig: pygit2.Signature, message: str) -> str:
    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    parents = [repo.head.target] if not repo.head_is_unborn else []
    return str(repo.create_commit("HEAD", sig, sig, message, tree, parents))


def _server_script(answer_marker: str) -> str:
    """One-file HTTP server: `/health` -> 200 ok; `/chat` -> 200 with the
    branch's answer marker. Branches differ only in this marker, which the
    evaluator then asserts against."""
    return textwrap.dedent(
        f"""
        import json, sys
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
                    self._send(json.dumps({{"answer": "{answer_marker}"}}))
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                self._send(json.dumps({{"answer": "{answer_marker}"}}))
            def log_message(self, *args): pass
        HTTPServer(("127.0.0.1", port), H).serve_forever()
        """
    )


def _init_three_branch_repo(repo_path: Path) -> None:
    """One repo with three branches that ship distinct server.py files:

      - `pass`:    server says 'success'   (would-pass any contains_text=success eval)
      - `fail`:    server says 'broken'    (would fail same eval)
      - `partial`: server says 'success-partial' (passes contains_text=success but
                   fails contains_text=success-only)
    """
    pygit2.init_repository(str(repo_path), initial_head="main")
    repo = pygit2.Repository(str(repo_path))
    sig = pygit2.Signature("test", "test@example.com", 0, 0)

    (repo_path / "server.py").write_text("")
    _commit(repo, sig, "init")

    main_branch = repo.branches.local["main"]
    for branch_name, marker in (("pass", "success"), ("fail", "broken"), ("partial", "success-partial")):
        repo.checkout(main_branch)
        branch = repo.branches.local.create(branch_name, repo[repo.head.target])
        repo.checkout(branch)
        (repo_path / "server.py").write_text(_server_script(marker))
        _commit(repo, sig, f"branch {branch_name}")
    # Park the main worktree back on `main` so none of the branches under
    # test are currently checked out — pygit2 won't add a worktree for a
    # branch that's already checked out anywhere.
    repo.checkout(main_branch)


class _ContainsTextRecorder:
    """Minimal evaluator: records the variant + final_answer it saw, returns
    pass/fail based on whether `must_contain` appears in the answer."""

    def __init__(self, must_contain: str) -> None:
        self.must_contain = must_contain
        self.seen: list[tuple[str, str]] = []

    async def __call__(self, variant_name: str, final_answer: str) -> bool:
        self.seen.append((variant_name, final_answer))
        return self.must_contain in final_answer


def _start_command() -> list[str]:
    # Each branch's server.py is identical in shape but differs in the answer
    # marker — git_branch executes this script in the worktree, so we just
    # exec the in-repo server.py.
    return [sys.executable, "server.py", "{port}"]


@pytest.mark.integration
async def test_three_branches_run_in_parallel_with_per_branch_deltas(
    tmp_path: Path,
) -> None:
    """End-to-end: three git_branch SystemAdapters, one per branch. Each
    variant dispatches concurrently; each variant's adapter brings up its own
    worktree + HTTP service; the evaluator records distinct answers per
    branch; per-variant rollup shows the expected pass/fail pattern."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_three_branch_repo(repo)

    case = EvalCase(id="case_alpha", input={"q": "ping"})

    async def run_one_branch(branch: str, expected_marker: str) -> tuple[str, str]:
        adapter = GitBranchAdapter(
            repo_path=str(repo),
            branch=branch,
            start_command=_start_command(),
            healthcheck="GET /health",
            healthcheck_timeout_seconds=10,
            inner_adapter="http",
            inner_config={
                "endpoint": "http://127.0.0.1:{port}/chat",
                "response_mapping": {"final_answer": "$.answer"},
            },
        )
        variant = RunVariant(name=branch, adapter="git_branch", config={})
        async with adapter:
            trace = await adapter.run(case, variant, None)
            return branch, trace.output.final_answer or ""

    started = utc_now()
    outputs = await asyncio.gather(
        run_one_branch("pass", "success"),
        run_one_branch("fail", "broken"),
        run_one_branch("partial", "success-partial"),
    )
    finished = utc_now()

    by_branch = dict(outputs)
    assert by_branch["pass"] == "success"
    assert by_branch["fail"] == "broken"
    assert by_branch["partial"] == "success-partial"

    # Per-branch delta against an evaluator that checks for "success" — the
    # `pass` branch passes, `partial` passes (substring match), `fail` fails.
    evaluator = _ContainsTextRecorder(must_contain="success")
    deltas = {
        branch: await evaluator(branch, by_branch[branch])
        for branch in ("pass", "fail", "partial")
    }
    assert deltas == {"pass": True, "fail": False, "partial": True}

    # Concurrency sanity: three branches with their own healthcheck loops
    # should still complete in well under "3x single-branch wall time".
    # A real serial run would be ~3x the per-branch startup; if we got
    # something close to that, parallelism is broken.
    wall_seconds = (finished - started).total_seconds()
    assert wall_seconds < 25.0, f"branches ran serially? wall={wall_seconds:.1f}s"
