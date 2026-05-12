# Roadmap

A roadmap is a series of "what's the next-smallest thing that's still useful." We define each milestone by what it lets a user do, not by which lines of code change.

The thesis (from [README](../README.md)) does not change between milestones:

> **The trace is the system of record. The config is the contract.**

Every milestone adds extension points or tightens the core. None of them rewrite the runner.

---

## v0 — "It runs"

**You can describe one eval in YAML and run it.**

### Scope

- CLI: `evalh run configs/eval.yaml`
- Dataset: `yaml`
- System adapters: `http`, `python_function`
- Evaluators: `contains_text`, `tool_called`, `llm_judge` (with `nl_assertions` and `rubric` modes; Anthropic backend via `[anthropic]` extra), `exact_match`
- Trace store: `local_files` only (`runs/<run_id>/`)
- Workspace: `tempdir_snapshot` (no git required)
- Variants: endpoint variants (Mode 1 from [Variants.md](Variants.md))
- Async runner with `max_concurrency`
- Per-evaluator and per-cell failure isolation
- `summary.yaml` with per-variant pass-rates and a baseline `ComparisonReport`

### Out of scope (deliberately)

- SQLite / Postgres trace store
- Branch / docker system adapters
- Filesystem-modifying evaluators (`git_diff`, `command`)
- CLI sub-commands beyond `run`
- Web UI

### Done when

- `examples/tiny_demo/` runs end-to-end on a fresh checkout in under 60 seconds against a real Anthropic API call (cheap model). Requires `ANTHROPIC_API_KEY`. Validates that the harness actually evaluates a stochastic system, not just that plumbing works.
- `examples/listing_price/` validates (loads + plans without error) but is not executed in CI; it's the realistic-shape reference that requires a user-provided agent.
- Unit tests under `tests/unit/` cover deterministic correctness with mocked transports and stub callables — that's where the no-network testing happens.
- A new evaluator can be added in < 80 lines including tests.
- A new HTTP-shaped system can be evaluated by editing only `eval.yaml`.

---

## v0.1 — "It re-evaluates and inspects"

**You can rerun evaluators on existing traces. You can compare runs. You can write a custom evaluator without forking the package.**

### Adds

- CLI:
  - `evalh re-evaluate <run_dir>` — re-run evaluators against stored traces
  - `evalh inspect <run_dir>` — pretty-print a case's trace and results
  - `evalh compare <run_a> <run_b>` — diff two runs at the case level
- Trace store: `sqlite` (queryable from notebooks)
- Dataset adapter: `jsonl`
- System adapter: `cli` (subprocess)
- Evaluators: `schema_match`, `latency_under`, `cost_under`
- Workspace: `git` (opt-in; uses `git diff` for richer artifacts)
- Reports: human-friendly markdown summary
- Entry-point-based plugin loading documented in `Adapters.md` and `Evaluators.md`

### Done when

- A user adds a custom `sql_equivalent` evaluator in their own package and uses it from `eval.yaml` without modifying eval-harness.
- `evalh re-evaluate` produces identical results to a fresh run when no evaluator code changed.

---

## v0.2 — "It runs at team scale"

**Multiple engineers can share evals; CI can run them.**

### Adds

- Trace store: `postgres`
- Run-id namespacing per project / per branch
- CI integration recipe: GitHub Actions workflow that runs evals on PR and posts a summary comment
- Cost guardrails: `run.cost_limit_usd` aborts a run if exceeded
- Re-run-only-failed: `evalh run --retry-only-failed <run_id>`
- Trace pagination + streaming for large runs (10K+ cases)

### Done when

- A PR comment shows pass-rate delta vs `main` for every PR touching the agent.
- A 10K-case run finishes without OOMs on a CI runner.

---

## v1 — "It tests systems that mutate the world"

**Coding agents, infra agents, anything that modifies a workspace.**

### Adds

- System adapter: `git_branch` (Mode 2)
- System adapter: `docker`
- System adapter: `replay` — for online evaluation against historical traces (no system call)
- DatasetAdapter `embed_full_trace: true` mode for `langfuse` / `phoenix` (and others) — fetches outputs alongside inputs so `replay` variants can score what already shipped
- Workspace adapter: `docker_volume` (sandboxed)
- Evaluators: `git_diff`, `command`, `semantic_similarity`
- Evaluators for thinking: `thinking_tokens_under`, `thinking_present`, `thinking_does_not_leak`
- Filesystem evals: artifact viewer in `evalh inspect`
- Multi-turn user simulator (system adapter wraps a simulated-user loop)
- Cost tracking from price tables (when the system doesn't emit cost itself)
- Three eval modes documented as first-class: offline (curated cases), backtesting (historical inputs against fresh systems), online (replay historical traces, no system call)

### Done when

- A coding-agent eval runs against three branches in parallel and produces per-branch test-pass deltas.
- The `docker_volume` workspace prevents an evaluator from reading the user's home directory (verified in CI).

---

## v1 supplement — "It plugs into observability platforms"

**Eval Harness coexists with Langfuse, Phoenix, Arize, and any OTel-compatible backend. See [Observability.md](Observability.md) for full design.**

### Adds (in order — highest leverage first)

1. **OTel `TraceStore`** — one adapter, multi-platform reach (Honeycomb, Datadog, Tempo, Phoenix, OTel-mode Langfuse).
2. **OTel `TraceEnricher`** — read upstream spans by `trace_id` from any OTel-queryable backend.
3. **Streaming SystemAdapter support** — SSE / chunked JSON / websocket; captures TTFT, tokens/sec, stream completion. New evaluators: `latency_first_token_under`, `tokens_per_second_above`, `stream_completed`.
4. **`TraceEnricher` adapter family** — fetch upstream traces from Langfuse / Phoenix / Arize and merge into our `Trace`. Failure-soft.
5. **Multi-sink `output:`** — `output:` becomes a list. Local files stays canonical; remote sinks mirror. Sink failures don't abort the run.
6. **`langfuse` triplet** — `DatasetAdapter` (production traffic as cases), `TraceStore` (mirror), `TraceEnricher` (rich upstream).
7. **`phoenix` triplet** — same shape; Phoenix is OTel-native so most work composes with the OTel adapter.

### Done when

- A user can take any existing Langfuse / Phoenix project and run Eval Harness against the variants without exporting data manually.
- An OTel-emitting system shows up in Honeycomb with eval traces grouped by `run_id` and `case_id`.
- A streaming agent's TTFT regressions show up in the per-variant comparison report.

## v1.x — "It plugs into the rest of the ecosystem"

### Adds

- Platform triplets: `arize`, `braintrust` (Dataset + TraceStore + TraceEnricher). Arize composes OTel (thin layer; no parallel exporter stack).
- Dataset adapter: `helicone` (REST-only; httpx in core).
- Drift detection: `evalh promote` (atomic symlink at `runs/baselines/<eval>/`) + `evalh drift` (compares against baseline, writes `drift.yaml` with `ComparisonReport(kind='drift')`; `--exit-nonzero-on-regression` gates CI).
- Webhook TraceStore: `slack`, `discord`, `linear`. Drift-aware: when the run carries `kind='drift'`, the formatted message highlights regressions + pass-rate Δ.
- Scheduled-run recipe: `templates/eval-daily.yml` + `docs/CI.md → "Scheduled runs with drift alerts"`.
- *(replay against a different system variant — already shipped in v1: `replay` SystemAdapter + `embed_full_trace` DatasetAdapter pattern, demonstrated by [`examples/online_eval/`](../examples/online_eval/). Listed here for completeness; no separate v1.x bead.)*

### Done when

- A scheduled run posts a daily Slack summary with regression highlights.
- A team using Arize as their production observability platform can run evals against production traffic without touching CSV exports.

---

## v2 — "Distributed" — shipped 2026-05-12

**The unit of distribution is a cell. The runner becomes a coordinator.**

### Adds (all landed)

- **Executor abstraction**: `local` (default, preserves the v1.x async runner), `ray`, `modal`, `celery`, `kubernetes` — each registers via the `eval_harness.executors` entry-point group. The runner only ever calls the Protocol; no `isinstance` branching survives.
- **CellDescriptor + deterministic `cell_id`s**: workers rebuild adapters from `eval_config_dict` via the existing factory + entry-point layer. Config travels; code doesn't.
- **ObjectStorage** (`fsspec`): one class behind `file://`, `s3://`, `gs://`, `az://`, `memory://` — per-cell artifacts move through it. Cloud backends are extras (`[s3]` / `[gcs]` / `[azure]`).
- **Idempotent cell IDs**: `local_files` / `sqlite` / `postgres` enforce the `save_trace_idempotent` contract so a coordinator can resume after worker crashes. `--retry-only-failed` picks up cells with no Trace at all (the worker-crashed-mid-cell case), not just `Trace.error` rows.
- **Capacity pools** in the local executor — different variants can target different semaphores via `run.executor.config.pools` + `systems[].pool`.

### Done when (verified)

- 10K-case fixture runs against the local executor within 5% of the v1.x baseline (`tests/perf/test_local_executor_perf.py`).
- 1K-case fixture runs through an in-process Ray cluster (`@pytest.mark.ray`).
- The full test suite passes unchanged across executors — no evaluator / adapter changes required.
- 1M-case aspirational target: documented as a manual benchmark (`benchmarks/distributed_1m.py`), not a CI gate. Maintainers run it against a real Ray cluster when they want the number; the CI envelope is the 10K perf gate.

---

## Forever-maybe

These are explicitly *not* on the path until the case is overwhelming. Listing them makes it clear they're out of scope:

- Hosted SaaS
- Web UI / dashboard (would compete with Langfuse, Arize, Phoenix; not our value-add)
- Auth / RBAC / multi-tenancy
- A built-in dataset library (we are infra, not content)
- Agent-building features (we evaluate agents; we don't build them)
- Streaming evals on production traffic (separate tool)
- Visual diff editor for prompts

If a user asks for one of these, the answer is "another tool does that better; here's how to plug it in."

---

## Decision principles

When choosing what lands next:

1. **Boring beats clever.** A new adapter beats a new framework.
2. **Trace shape is a constitution.** We do not break it. Additive changes only.
3. **The runner does not grow.** If a feature requires runner changes, it's the wrong feature or the wrong design.
4. **Each milestone is shippable independently.** v0.1 does not require v0.2 to be useful.
5. **Real users over hypothetical ones.** A feature lands when one team is blocked on it. Not before.
