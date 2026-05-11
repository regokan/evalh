# Coding rules — deltas

Generic Python style (PEP 8, PEP 484, PEP 604/585, `from __future__ import annotations`) is enforced by `ruff` and `mypy --strict`. This file is just the project-specific deltas.

## Async-everywhere — what "everywhere" means

- Every `Adapter` / `Evaluator` / `Store` method is `async def`.
- Use `httpx.AsyncClient`, never `requests`.
- Use `aiofiles` for file I/O in the runtime path. Sync `open()` is fine in CLI argument parsing or one-shot helpers, never in the hot path.
- Third-party SDK only sync? Wrap with `asyncio.to_thread(...)` *inside the adapter*, not in the runner.
- `time.sleep` → `await asyncio.sleep`. Threads for parallelism → no; use `asyncio.gather`.

## Naming

- `*Adapter`, `*Evaluator`, `*Store`, `*Factory` suffixes mandatory.
- Config files always `eval.yaml` / `cases.yaml`. Never `config.yaml` / `dataset.yaml`.
- Run IDs: `{ISO8601}_{eval_name}` — sortable.

## Types

- `mypy --strict` passes. `Any` in a public signature requires a `# why-Any:` comment.
- Pydantic v2 for all data models in `eval_harness/core/models.py`.
- Adapter families are `Protocol`-typed in `eval_harness/core/`. Implementations subclass implicitly.

## Comments

Default to writing none. A comment justifies its existence by recording a *why* that's not obvious from the code — a hidden invariant, a workaround for a specific upstream bug (with a link), a constraint the next reader would reasonably question. If removing the comment wouldn't confuse a future reader, don't write it.

No multi-paragraph docstrings. No "Added for the X flow" — that's PR-description content.

## Errors

Custom errors live in `eval_harness/core/errors.py`. Use them; don't raise plain `Exception`.
- `ConfigError` — at plan time, before any case runs.
- `AdapterError` — at run time; runner catches and turns into `Trace.error`.
- `RetriableError(AdapterError)` — only these get retried.

Don't broad-catch `except Exception`. Catch specific types or let it propagate.

## Don't pre-build

A bug fix doesn't need a refactor. A new evaluator doesn't need a new abstraction layer. Three similar lines is better than a premature `_helper`. If the next change can't reuse the abstraction, the abstraction was wrong.
