# Changelog

All notable changes to this project are recorded here. Schema: per-release
sections in reverse chronological order. v0.x lines map 1:1 to the spec
in [`docs/Roadmap.md`](docs/Roadmap.md).

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
