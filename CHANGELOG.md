# Changelog

All notable changes to this project are recorded here. Schema: per-release
sections in reverse chronological order. v0.x lines map 1:1 to the spec
in [`docs/Roadmap.md`](docs/Roadmap.md).

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
