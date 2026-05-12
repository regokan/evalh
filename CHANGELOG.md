# Changelog

All notable changes to this project are recorded here. Schema: per-release
sections in reverse chronological order. v0.x lines map 1:1 to the spec
in [`docs/Roadmap.md`](docs/Roadmap.md).

## v2 — 2026-05-12

**"Distributed."** The unit of distribution is a cell. The runner
becomes a coordinator.

**Executor Protocol + cells**

- `eval_harness.core.executors.base.Executor` Protocol: `open(plan)` →
  `bind_cells(...)` → `dispatch_all(cells)` → `finalize()` → `close()`.
  The runner only ever calls the Protocol; no `isinstance` branching
  on executor type lives there.
- `CellDescriptor` carries per-cell config (variant, case, eval_config
  dict, optional pool / workspace kind). Deterministic `cell_id` via
  `compute_cell_id(...)` — same inputs across machines hash to the same
  id, which is what the trace-store idempotency check leans on.
- Trace stores ship `save_trace_idempotent(trace, cell_id) -> bool`.
  Three canonical sinks enforce: `local_files` (sidecar marker),
  `sqlite` (`cell_id TEXT` column + `error_type` guard on UPSERT),
  `postgres` (indexed `cell_id` + `ON CONFLICT WHERE existing.error_type
  IS NOT NULL`). Other stores inherit the always-write fallback.

**Local executor (default)**

- `eval_harness.core.executors.local.LocalExecutor`: wraps the v1.x
  `asyncio.gather` + `asyncio.Semaphore` hot path under the Protocol.
  Default `run.executor.type` is `local` — no breaking config change.
- **Capacity pools**: `run.executor.config.pools = {name: int}` declares
  named pools; `systems[].pool` routes a variant to one. Absent routing
  falls back to per-variant / global semaphores.
- 10K-case perf gate (`tests/perf/test_local_executor_perf.py`) asserts
  wall-time stays within 5% of the pre-v2 baseline captured in
  `tests/perf/baselines/local_executor_10k.json`.

**ObjectStorage**

- `eval_harness.core.object_storage.fsspec_storage.FsspecObjectStorage`:
  one class behind `file://`, `s3://`, `gs://`, `az://`, `memory://`.
  Cloud backends ship as extras (`[s3]`, `[gcs]`, `[azure]`).
- Registry under `eval_harness.object_storages`; default builder roots
  at `runs/<run_id>/artifacts/` for the local-only path.

**Distributed executors**

- `ray` — each cell becomes a `ray.remote` task. In-process mode for the
  integration test (`@pytest.mark.ray`); real cluster runs are the
  manual benchmark.
- `modal` — each cell becomes a `modal.Function` call. Cloud-only;
  smoke gated on `MODAL_TOKEN_ID` + `~/.modal.toml`.
- `celery` — each cell becomes a Celery task. Redis broker by default;
  CI integration test gated on `EVALH_TEST_REDIS_URL`.
- `kubernetes` — each cell becomes a `batch/v1` Job whose pod runs the
  new `evalh-cell-worker` console-script. Payload routing: env var for
  ≤ 32 KiB, ConfigMap fallback above. Result piping through
  ObjectStorage (`cells/<cell_id>/outcome.json`). Smoke gated on
  `EVALH_TEST_K8S_CONTEXT` + `kubectl get pods`.
- Distributed executors emit a one-shot warning at `open()` when paired
  with the single-writer `local_files` store. See
  `docs/Adapters.md > Trace store concurrency safety`.

**Retry across executors**

- `--retry-only-failed` now picks up cells that have NO Trace at all
  (worker-crashed-mid-cell), not just `Trace.error` rows. The retry
  path widens its scan to the plan's expected `(case, variant)` set
  and includes cells absent from `traces.jsonl`.

**Docs + benchmarks**

- `docs/Executors.md` — Protocol, cell idempotency, deployment recipes
  for all four distributed executors.
- `docs/Adapters.md > Trace store concurrency safety` — per-store
  table covering which executor / store combos are safe.
- `docs/CI.md > Distributed executors in CI` — which markers CI runs
  (Ray, Celery + Redis service) and how to run the others locally
  (Modal, Kubernetes).
- `benchmarks/distributed_1m.py` + `benchmarks/README.md` — maintainer
  script for the 1M-case-against-200-worker-Ray-cluster aspirational
  target. Not a CI gate.

## v1.x — 2026-05-12

**"It plugs into the rest of the ecosystem."** Drift detection,
webhook reporting, three more observability platforms.

**Drift detection**

- `eval_harness.runner._deltas` — shared pure-function primitives
  (`pass_map`, `compute_pass_rate_delta`, `compute_regressions`,
  `compute_improvements`, `compute_evaluator_deltas`,
  `compute_latency_cost_deltas`). Same arithmetic powers within-run
  variant comparison (`ComparisonReport(kind='ad_hoc')`) and
  across-run drift detection (`kind='drift'`) — DRY win.
- `eval_harness.core.baseline` — symlink-based baseline marker.
  `runs/baselines/<eval_name>/` is the source of truth; atomic
  replace via write-temp-then-rename, relative-target symlinks
  survive runs-tree moves.
- `ComparisonReport` additive fields: `kind`, `baseline_run_id`,
  `regressions_count`, `improvements_count`. Defaults preserve
  backwards compat — existing `summary.yaml` files load unchanged.
- **CLI**: `evalh promote <run_dir>` creates/replaces the symlink;
  `evalh drift <run_dir>` resolves the baseline (explicit
  `--baseline` > promoted symlink > graceful "no baseline" notice),
  prints markdown to stdout, writes `<run_dir>/drift.yaml`.
  `--exit-nonzero-on-regression` gates CI while still persisting
  the report.

**Webhook TraceStore**

- `output: webhook` posts a per-run summary to Slack / Discord /
  Linear. `save_summary` is the only meaningful hook; per-cell
  hooks no-op (webhook reporting is summary-grained).
- Canonical `SummaryMessage` struct + `format_slack` /
  `format_discord` / `format_linear` formatters keep Block Kit /
  embed / GraphQL quirks out of the builder.
- Drift-aware: when the run carries `comparison.kind='drift'`, the
  formatted message highlights regression / improvement counts,
  pass-rate Δ, and the top regression case IDs (Slack warning
  emoji, Discord red color, Linear "Top regressions" markdown).
- Failure-soft via v1-supplement's multi-sink path —
  non-first-sink webhook failures land on `RunSummary.sink_errors`
  rather than aborting the run.
- Slack + Discord use plain httpx POSTs (no SDK). Linear uses the
  `linear-api` SDK via the new `[webhook]` extra.

**Three new platform integrations**

- **Arize** (triplet): `ArizeTraceStore(OtelTraceStore)` — thin
  subclass that adjusts endpoint + resource attributes; span
  emission is inherited unchanged. Shares the underlying
  `OtelClient` registry from v1-supplement — no parallel exporter
  stack. Plus `ArizeDatasetAdapter` (`embed_full_trace` -> replay)
  and `ArizeTraceEnricher`. `[arize]` extra; `arize-otel` SDK is
  ceremonial (httpx + OtelClient cover everything).
- **Helicone** (single adapter): `HeliconeDatasetAdapter` pulls
  historical request logs via the `Helicone-Auth: Bearer <key>`
  REST API. `[helicone] = []` marker extra (REST-only, httpx in
  core).
- **Braintrust** (triplet, parallel polecat).

**Scheduled-run recipe**

- `templates/eval-daily.yml` — reference GitHub Actions workflow
  consumers copy. Cron + workflow_dispatch -> `evalh run` ->
  resolve newest run dir -> `evalh drift
  --exit-nonzero-on-regression` -> webhook sink in
  `eval.yaml > output:` -> artifact upload regardless of
  pass/fail. Documented in
  `docs/CI.md → "Scheduled runs with drift alerts"`.

**Boundary security**

- `eval_harness/core/url.py.validate_url_scheme` — shared helper
  used by `http_adapter` and `webhook_trace_store`. Rejects
  `file://`, `gopher://`, plain `http://` to non-localhost, etc.
  Keeps adapters from drifting on the SSRF defense.

## v1-supplement — 2026-05-12

**"It plugs into observability platforms."** Eval Harness coexists with
Langfuse, Phoenix (Arize), and any OTel-compatible backend (Tempo,
Honeycomb, Datadog, Jaeger, Grafana).

**Sixth adapter family**

- `TraceEnricher` — Protocol + factory + runner integration. Enrichers
  run between the SystemAdapter and evaluators; failures are caught and
  recorded on `trace.extra.enrichment_errors` rather than aborting the
  cell. The load-bearing failure-soft invariant for production observability
  hiccups.

**OTel pair**

- `OtelTraceStore` — pushes spans (root per `(case, variant)`, children
  per tool call, sibling spans per evaluator result, one run-summary
  span). Write-only by design; pair with a queryable canonical sink.
- `OtelTraceEnricher` — fetches upstream spans via a `{trace_id}` URL
  pattern with bounded ingestion-lag retry, JSONPath merge rules into
  any Trace path.
- `_platforms/otel.py` — shared `OtelClient`. Reference-counted
  registry keyed on (endpoint, headers, protocol, resource_attributes)
  so multiple OTel-shaped adapters in the same run share one
  `TracerProvider`.

**Langfuse triplet**

- `LangfuseDatasetAdapter`, `LangfuseTraceStore`, `LangfuseTraceEnricher`
  + shared `_platforms/langfuse.LangfuseClient`. Refcounted instance per
  `(host, api_key)`; tests inject a programmable in-memory SDK with a
  deterministic clock so ingestion-lag scenarios are reproducible.

**Phoenix triplet**

- `PhoenixTraceStore(OtelTraceStore)` — *thin* subclass: only the
  endpoint (`<base>/v1/traces`) and resource attributes
  (`openinference.project.name`) differ. Span emission logic is the
  parent class's, unchanged. Two PhoenixTraceStores or a Phoenix +
  plain OTel store with matching targets share a single
  `TracerProvider` via the platform registry.
- `PhoenixTraceEnricher`, `PhoenixDatasetAdapter` — both run over
  Phoenix's REST API via `_platforms/phoenix.PhoenixClient` (httpx
  query side + OtelClient push side). `[phoenix]` extra adds
  `arize-phoenix-otel` for users who want OpenInference
  instrumentation alongside.

**Multi-sink output**

- `output:` accepts a list of TraceStores. The first sink is canonical
  (failures abort the run); the rest are best-effort mirrors —
  failures land on `RunSummary.sink_errors`. Single-mapping
  `output:` still validates and coerces to a one-element list for
  backwards compatibility with v0/v0.1 configs.

**Streaming HTTP**

- HTTP SystemAdapter `stream: true` mode parses SSE chunks, records
  `TraceMetrics.latency_first_token_ms`, `latency_last_token_ms`,
  `tokens_per_second`, `stream_chunks`, `stream_completed`.
- Three streaming-only evaluators ship: `latency_first_token_under`,
  `tokens_per_second_above`, `stream_completed`.

**Tests**

- All three platform families have hermetic test suites:
  `InMemorySpanExporter` for OTel-shaped sinks, `respx` for HTTP
  query APIs, deterministic clock for ingestion-lag retry.
- New integration test wires `fixture` DatasetAdapter ->
  `replay` SystemAdapter -> multi-sink (`local_files` + OTel)
  end-to-end; the fake collector receives every span with
  `run_id` + `case_id` attributes.
- `git_branch` worktree naming switched from `id(branch_ref)` to a
  `uuid4()`-derived suffix — fixes a concurrent-thread collision
  surfaced by the v1 three-branch integration test.

## v1 — 2026-05-12

**"It tests systems that mutate the world."** Coding agents, infra agents,
anything that modifies a workspace.

**New SystemAdapters**

- `git_branch` — checks out a branch into a worktree, starts the
  service from `start_command`, polls a healthcheck, then delegates
  every call to a composed `inner_adapter` (typically `http`). The
  unit of variation is a branch.
- `docker` — same composition pattern, image instead of branch. Pulls
  on missing, polls a healthcheck, stop+remove on exit. Sync docker
  SDK calls wrapped with `asyncio.to_thread`.
- `replay` — returns the `Trace` already embedded in the case by an
  `embed_full_trace`-mode DatasetAdapter. No system call. The
  evaluator pipeline runs against historical traffic.
- `user_simulator` — wraps any inner adapter in a multi-turn
  simulated-user loop driven by an `LlmBackend`.

**Workspace**

- `docker_volume` — sandboxed workspace. The system runs inside a
  container with a single bind-mounted volume; nothing outside the
  volume is visible. Security test: a container started by this
  adapter MUST NOT be able to read the host's `$HOME/.ssh`.

**DatasetAdapter**

- `embed_full_trace` Protocol — adapters opt in to attaching the
  source trace to each case. `fixture` (new) implements it for
  offline tests; production platform adapters (langfuse, phoenix)
  pick it up in v1-supplement.

**New evaluators**

- `git_diff` — asserts `must_modify_files` / `must_not_modify_files`
  against `FilesystemArtifact.diff`; optional `expected_patch_path`
  exact-string compare.
- `command` — runs a subprocess (`shell=False`, `cwd='artifact'`
  by default, bounded `timeout_seconds`, captured + capped output).
- `semantic_similarity` — cosine similarity with a pluggable
  embedder backend. NO default ships: install `[openai]` or
  `[embeddings_local]`.
- `thinking_tokens_under`, `thinking_present`, `thinking_does_not_leak`
  — target the `output.thinking` field as a first-class evaluation
  surface.

**Core**

- `eval_harness.core.llm_backends` — runner-shared LLM dispatch
  registry. `LlmBackend` Protocol + `LlmCall` model with token /
  cost fields. `llm_judge`, `user_simulator`, and
  `thinking_does_not_leak` share one backend.
- `eval_harness.core.price_tables` — versioned, dated `PriceTable`
  with a `DEFAULT_PRICE_TABLE` (~5 current-gen models, source URLs
  in comments). The runner fills `Trace.metrics.cost_usd` from the
  table when adapters report tokens but not cost. User override via
  `metrics.price_table_path`; the runner warns once per run when
  the default is in use.

**Embedder registry**

- `eval_harness.evaluators._embedders` — shaped like `llm_backends`.
  Two reference backends ship behind extras: `OpenAIEmbedder`
  (`[openai]`) and `SentenceTransformersEmbedder`
  (`[embeddings_local]`, NEW extra, ~80MB local model).

**CLI**

- `evalh inspect --case <id>` now renders the `FilesystemArtifact`
  when present: diff summary table, per-file unified-diff bodies
  (`rich.Syntax`, 200-line truncation by default), workspace
  metadata. `--no-artifacts` skips even when present.

**Examples**

- `examples/coding_agent/` — Claude-Haiku patches a fixture repo;
  `command` evaluator runs pytest in the artifact directory.
  Smoke test (requires `ANTHROPIC_API_KEY`), not in CI.
- `examples/online_eval/` — fixture DatasetAdapter + replay
  SystemAdapter, fully offline. The shape works unchanged once
  langfuse / phoenix adapters land.

**Tests**

- Three new SystemAdapters share lifecycle + trace-shape coverage
  alongside `http` and `python_function`. New `@pytest.mark.docker`
  marker for tests that need a reachable daemon; skipped cleanly
  otherwise.
- Three-branch coding-agent integration test exercises `git_branch`
  concurrency and the per-variant rollup.

## v0.2 — 2026-05-12

**Backends & streaming**

- `postgres` TraceStore — `asyncpg` + JSONB payloads + server-side cursors
  so 100K-case runs stream rather than materialise. Behind the
  `[postgres]` extra.
- TraceStore Protocol gained four read methods (`iter_traces`,
  `iter_results`, `load_summary`, `list_run_ids`) so `local_files`,
  `sqlite`, and `postgres` are interchangeable for downstream tooling.
- `run_namespace: dict[str, str]` — config-level metadata for multi-tenant
  filtering. `local_files` ignores; `sqlite` stores it as a JSON column;
  `postgres` indexes on it. Additive — no `schema_version` bump.

**Runner**

- `run.cost_limit_usd` — soft run-level cost guardrail. After each cell,
  the accumulator sums `trace.metrics.cost_usd`; once the total reaches
  the limit, queued cells short-circuit with a `cost_limit`-typed Trace.
  In-flight cells finish naturally. Composes with the existing
  per-evaluator `cost_limit_usd` rather than replacing it.
- Streaming summary aggregation — `SummaryAggregator` consumes
  `CellOutcome`s in a single pass with fixed-size counters per variant +
  per (evaluator, variant), so summary memory no longer scales with case
  count.

**CLI**

- `evalh run --retry-only-failed RUN_DIR` reuses an existing `run_dir` /
  `run_id` and re-executes only the cells whose Trace recorded an error;
  `--include-evaluator-failures` widens the retry set to cells that ran
  successfully but had at least one failing or erroring evaluator.

**CI**

- `.github/workflows/ci.yml` — ruff + mypy --strict + pytest matrix
  (Python 3.11 / 3.12 / 3.13) on push/PR via pinned `uv`. Concurrency
  group cancels superseded runs.
- `.github/workflows/smoke.yml` — `workflow_dispatch`-only smoke against
  real Anthropic, uploads the run directory as an artifact.
- `templates/eval.yml` — reference recipe for consumers; cross-linked
  from `docs/CI.md`.

**Performance**

- `tests/perf/test_10k_case_run.py` — `@pytest.mark.perf`-gated guard
  that runs 10K cases x 3 variants through the streaming aggregator and
  asserts the wall-clock and memory budget. CI runs `-m "not perf"` by
  default; opt-in with `pytest -m perf tests/perf/`.

## v0.1 — 2026-05-12

**Adapters & stores**

- `cli` SystemAdapter — run a CLI agent as a subprocess. `shell=False`
  always, bounded timeout via `asyncio.wait_for` (`TimeoutError →
  RetriableError`), host env is not inherited unless `env` is set
  explicitly. Optional `stdin_template` with `{{ input.foo }}`
  substitution; `parse_stdout_as: text|json`.
- `jsonl` DatasetAdapter — one EvalCase per line, mirrors the YAML adapter's
  schema_version + duplicate-id semantics.
- `sqlite` TraceStore — append-mostly store backed by `aiosqlite`. One
  table per data type, JSON payload column queryable with
  `json_extract(...)`. Behind the `[sqlite]` extra.
- `git` WorkspaceAdapter — clone-then-prepare via `pygit2`; emits the same
  `FilesystemArtifact` shape as `tempdir_snapshot`. Behind the `[git]` extra.

**Evaluators**

- `schema_match` — `jsonschema.Draft202012Validator` over a JSONPath into
  the trace (default `output.structured`). New core dep:
  `jsonschema>=4,<5`.
- `latency_under` — pass when `trace.latency_ms < max_ms`.
- `cost_under` — pass when `trace.metrics.cost_usd < max_usd`. Missing cost
  is reported explicitly (`passed=false`, "cost not reported by adapter")
  rather than silently passing.

**CLI**

- `evalh inspect <run_dir>` — render traces / results for a run (with
  `--case` / `--variant` filters).
- `evalh re-evaluate <run_dir> [--add NAME]` — offline rescoring of
  existing traces against the run's original evaluators. Appends to
  `results.jsonl`. Deterministic evaluators are idempotent.
- `evalh compare <run_a> <run_b>` — cross-run diff: per-(case,variant)
  regressions / improvements, per-variant and per-evaluator pass-rate
  deltas, cases-only-in-one-run. Always exits 0 (informational).

**Reports**

- Plain-markdown report writer in `eval_harness.reports.markdown_writer`.

**Foundations**

- `eval_harness.core.run_reader.RunReader` — single shared reader for
  `runs/<id>/{config.yaml, traces.jsonl, results.jsonl, summary.yaml}`.
  All v0.1 CLI commands and downstream tooling go through it.

**Plugins**

- End-to-end integration test that loads a third-party adapter purely via
  the `eval_harness.*` entry-point groups — no source modifications
  required.

**Toolchain**

- Bumped `ruff` dev pin from `^0.7` to `^0.9`; newer ruff correctly
  recognises `pytest.importorskip(...)` as control flow and stops flagging
  the subsequent imports as `E402`.

## v0.0.1 — 2026-05-11

Initial v0 release: async runner, plan builder, YAML dataset adapter,
`local_files` trace store, `tempdir_snapshot` workspace, HTTP and
`python_function` system adapters, `contains_text` / `tool_called` /
`exact_match` / `llm_judge` evaluators, `evalh run` CLI.
