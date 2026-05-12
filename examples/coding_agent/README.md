# Example: coding_agent

A code-modifying agent eval. The agent receives a stub Python file with a TODO + a failing test, asks Claude for a patch, writes it back to a sandboxed copy of the repo, and the `command` evaluator runs `pytest` against the resulting tree to grade whether the agent's edit actually fixed things.

This is the canonical shape for "the agent under test mutates files in a workspace; the evaluator runs a command against that workspace."

## Files

| File | What it is |
|---|---|
| [`fixture_repo/calc.py`](fixture_repo/calc.py) | Tiny module with `add` (works) + `subtract` (raises `NotImplementedError`). |
| [`fixture_repo/test_calc.py`](fixture_repo/test_calc.py) | Pytest for both functions. `test_subtract` fails on the unmodified fixture. |
| [`agent.py`](agent.py) | The agent — reads the file, calls Claude, writes back. ~100 lines. |
| [`cases.yaml`](cases.yaml) | One case: fix `subtract`. |
| [`eval.yaml`](eval.yaml) | Wires `python_function` agent + `tempdir_snapshot` workspace + `command` evaluator (pytest). |

## Required environment

```bash
export ANTHROPIC_API_KEY=...
# or put it in examples/coding_agent/.env (gitignored)
```

## Run it

```bash
evalh run examples/coding_agent/eval.yaml
```

Expected runtime: well under 90s for the one shipped case.

## What happens, in order

1. `tempdir_snapshot` copies `fixture_repo/` into a fresh temp directory.
2. `python_function` adapter invokes `agent.run(case, variant)`. The adapter exposes the working copy at `variant["_workspace_path"]`.
3. The agent reads the target file, asks Claude to return a JSON patch (`{"path": "...", "content": "..."}`), and writes the new content back into the workspace.
4. The workspace adapter snapshots the post-edit tree as a `FilesystemArtifact` (artifact dir = the temp dir's grandchild — that's the path the `command` evaluator's `cwd` defaults to).
5. The `command` evaluator runs `python -m pytest -q` in the artifact dir. Pass iff exit code 0.

## Why this works as a smoke gate

It exercises the full v1 stack: a stochastic agent, a real filesystem workspace, a deterministic command-based evaluator. If any wire is loose — adapter doesn't expose workspace, tempdir doesn't snapshot, command can't find pytest — this run breaks. CI does not run it (cost + key); run it locally before tagging a release.

## Extending it

- Add cases by appending to `cases.yaml`. Each case specifies `target_file` + `task` (free-form prompt).
- Swap `claude_coder` for a different model by changing `_MODEL` in [`agent.py`](agent.py) — the model family must be registered in `eval_harness.llm_backends`.
- For a heavier setup that branches a real git repo per case, swap the workspace to `type: git` and the system adapter to `git_branch` (composes the same `python_function` inner adapter).
