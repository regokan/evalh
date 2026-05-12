# Contributing to eval-harness

Thanks for your interest. This document covers setup, the gates a PR must pass, and the architectural rails the project is built against.

## TL;DR

```bash
git clone https://github.com/regokan/evalh.git
cd evalh
uv sync --extra anthropic --extra sqlite --extra git
uv pip install pytest pytest-asyncio respx ruff mypy types-PyYAML jsonschema
uv run pytest tests/ -q
uv run ruff check .
uv run mypy --strict eval_harness/
```

If all three commands pass, your environment is good. Open a feature branch, make changes, repeat the three commands, push, open a PR.

## Toolchain

- **Python 3.11+** (matrix tested on 3.11 / 3.12 / 3.13 in CI).
- **uv** is the preferred package manager — `curl -LsSf https://astral.sh/uv/install.sh | sh`. Poetry also works (the lockfile is checked in for both flavors).
- **ruff** for linting/formatting.
- **mypy --strict** for type checking.
- **pytest + pytest-asyncio** for tests. Async test functions don't need a decorator (`asyncio_mode = "auto"` in `pyproject.toml`).

## How to find work

- `bd ready` (if you use [beads](https://github.com/gastownhall/beads) for issue tracking) shows unblocked open work in the local DB.
- GitHub Issues labeled **good first issue** are scoped to be picked up cold.
- A failing test in `tests/` with a `# TODO:` comment above it is fair game.
- New adapters, evaluators, embedder backends, or LLM-judge backends are always welcome — they slot in via the existing entry-point families without touching core.

## The four gates

Every PR must satisfy all four:

| Gate | Command | What it covers |
|---|---|---|
| Tests | `pytest tests/ -m "not perf and not docker and not ray and not modal and not celery and not kubernetes and not s3"` | The everyday suite. Currently 657+ tests. |
| Lint | `ruff check .` | Style + obvious bug-class checks. |
| Types | `mypy --strict eval_harness/` | Strict type coverage across 130+ source files. |
| Existing behavior | Existing examples (`examples/tiny_demo`, `listing_price`, `online_eval`, `coding_agent`) still validate. | No regressions in user-facing surface. |

CI runs these on every push and PR. Local runs catch problems faster.

Marker-gated tests (perf / docker / ray / modal / celery / kubernetes / s3) only run when their runtime is available. They must skip cleanly when it isn't, **except** the docker_volume sandbox test, which fails loudly when Docker is missing — see [`.claude/rules/security.md`](.claude/rules/security.md).

## Architecture rails

These constraints are load-bearing. The project survives because we hold them. If a change pushes against one of them, the change is usually the wrong shape.

### `runner/` stays boring

Anti-patterns the runner must never contain:

```bash
rg 'if .*adapter|if .*type|import (httpx|requests|anthropic|openai|git|yaml)' eval_harness/runner/
```

That grep must return nothing. If your change makes it return something, push the domain logic into a factory, adapter, or evaluator. See [`.claude/rules/architecture.md`](.claude/rules/architecture.md).

### Six adapter families. No seventh.

`SystemAdapter`, `DatasetAdapter`, `TraceStore`, `WorkspaceAdapter`, `TraceEnricher`, `EvaluatorFactory`. New platforms / runtimes slot in as types within these families. If you're proposing a seventh family, stop and propose it as a docs change first — the discussion belongs there.

### Async everywhere

- Every adapter / evaluator / store method is `async def`.
- `httpx.AsyncClient`, never `requests`.
- `aiofiles` or `asyncio.to_thread` for filesystem; never sync `open()` in the hot path.
- `time.sleep` → `await asyncio.sleep`. Threads for parallelism → no; use `asyncio.gather`.
- Third-party sync SDK? Wrap with `asyncio.to_thread(...)` *inside the adapter*, never in the runner.

See [`.claude/rules/coding.md`](.claude/rules/coding.md).

### Schemas are committed contracts

`Trace`, `EvaluationResult`, `RunSummary`, `FilesystemArtifact`, `DriftReport`, `CellDescriptor` — additive changes only inside a major version. The schema-bump checklist lives in [`docs/DataModel.md > Schema versioning`](docs/DataModel.md). Don't bump it without ticking all four boxes.

### Optional deps stay optional

`anthropic`, `openai`, `langfuse`, `phoenix`, `ray`, `modal`, etc. live under `[project.optional-dependencies]`. Core never imports them. Use `poetry add --optional --extras <name> <pkg>` for changes — never hand-edit `pyproject.toml` (the resolver caught a real bug last time).

### Testing layers

| Layer | Where | Network | API key | What it covers |
|---|---|---|---|---|
| **Unit** | `tests/unit/` | No (mocks) | No | One module in isolation |
| **Integration** | `tests/integration/` | No (respx + fake servers) | No | Multiple components, mocked transports |
| **Perf** | `tests/perf/` (`@pytest.mark.perf`) | No | No | 10K-case fixture; peak RSS + wall-time gates |
| **Smoke** | `examples/tiny_demo/`, `examples/coding_agent/` | Yes | Yes | Real-LLM end-to-end; manual, not CI |

Per-component minimums:

- **Each evaluator**: pass case + fail case + error case.
- **Each adapter**: open / run / close lifecycle + error path + (where applicable) the failure-soft contract.
- **Each factory**: unknown type → `ConfigError`; known type → instantiates.
- **Runner**: cells parallel + one-cell failure isolated + evaluator failure isolated + concurrency bounded.

Mocking conventions:

- **HTTP** → `respx.mock` with explicit route matchers; don't mock the whole client.
- **LLM judge / embedder** → inject a fake backend through the registry seam (`eval_harness.llm_backends`, `eval_harness.embedders`), not by patching SDK imports.
- **Filesystem** → `pytest`'s `tmp_path`.

See [`.claude/rules/testing.md`](.claude/rules/testing.md).

### Security

The full rules live at [`.claude/rules/security.md`](.claude/rules/security.md). The high-impact bits:

- Repo is public — anything committed is public forever. API keys go in env vars, never the repo.
- `subprocess` calls are `shell=False`, always. Bounded timeouts on every external call.
- The default `tempdir_snapshot` workspace is **not** a sandbox — use `docker_volume` for that, and the sandbox-can't-read-`$HOME` test gates the security claim.
- Replay traces may contain PII — let platform-side redaction handle it; don't strip after fetch.

## Commit & PR style

- **Conventional Commits**: `<type>(<scope>): <imperative summary>`, subject ≤ 72 chars. Types: `feat | fix | refactor | test | docs | chore | build | ci`. See [`.claude/rules/git.md`](.claude/rules/git.md).
- One PR per logical change. Squash on merge is fine.
- If a pre-commit hook fails, don't `--amend` — the failed commit didn't happen. Fix, re-stage, fresh commit.
- Force-push to `main` is forbidden. Force-push to your feature branch is your call.

## Documentation

User-facing changes ship with doc updates in the same PR:

- New evaluator? Update [`docs/Evaluators.md`](docs/Evaluators.md).
- New adapter type? Update [`docs/Adapters.md`](docs/Adapters.md).
- New config field? Update [`docs/ConfigSchema.md`](docs/ConfigSchema.md).
- New CLI surface? Update [`docs/CLI.md`](docs/CLI.md).
- New milestone-shaped feature? Add a `CHANGELOG.md` entry under the current version.

Internal-design changes (refactors with no user-visible surface) don't need doc updates, but a one-line note in the PR description explaining the why goes a long way.

## Filing issues

Use a clear title (`bug: <one-line symptom>` / `feat: <ask>` / `docs: <gap>`) and include:

- Version of `eval-harness` you're on (`pip show eval-harness`)
- The minimal `eval.yaml` that reproduces it (if relevant)
- Full error output, not just the last line
- What you expected vs. what you got

For security issues, please email rather than file a public issue — see [SECURITY.md](SECURITY.md) (coming soon).

---

Thanks for contributing. The project survives because of the rails it's built against; the rails survive because contributors take them seriously. Welcome.
