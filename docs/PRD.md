# PRD — Eval Harness

## Problem

Teams building AI systems (agents, RAG pipelines, code-modifying agents, multi-turn assistants) need to evaluate them repeatedly: across datasets, across model versions, across branches, across prompt revisions, across deployment environments. Existing options are either:

- **Heavy frameworks** (LangSmith, Arize, Langfuse) — useful, but coupled to their stack and tied to their data model.
- **One-off scripts** — fast to start, impossible to extend or compare across teams.
- **Benchmark codebases** (tau-bench, SWE-bench) — purpose-built for one domain, not reusable as infrastructure.

There is no minimal, boring, config-driven harness that runs *any* system against *any* dataset with *any* evaluator and gives you a clean comparison artifact.

## Product

**Eval Harness**: a Python package + CLI that takes a single YAML config, runs an AI system (or several variants of it) against a dataset, captures traces, runs evaluators, and writes results to a local folder.

It is the smallest serious eval system that still extends cleanly to:
- Branch comparison
- Filesystem-modifying agents (coding agents)
- Async multi-turn evaluations
- Pluggable storage (SQLite, Postgres, Langfuse) without touching the runner

## Non-goals (v0)

- Web UI / dashboard
- Hosted service
- Built-in dataset library
- Multi-tenant collaboration features
- Authentication, RBAC, audit logs
- Real-time streaming UI
- Agent-building framework (we evaluate agents; we do not build them)

These are explicitly deferred. Adding any of them in v0 makes the v0 design wrong.

## Users

| Persona | What they do | What they need |
|---|---|---|
| **AI engineer** building an agent | Iterates on prompts, tools, model choice | Run the same dataset on every iteration; see regressions |
| **AI infra engineer** | Owns the eval pipeline | Pluggable storage, CI integration, async at scale |
| **Researcher** | Compares techniques | Variant matrix; reproducible runs; portable trace format |
| **Eval author** | Writes the dataset and the assertions | Edit two YAML files, no Python required |

The v0 success bar: an eval author can ship a new eval by editing two YAML files. Zero Python.

## Functional requirements (v0)

### F1. One-config runs
A user runs `evalh run configs/eval.yaml`. That is the only command.

### F2. YAML-only authoring
Datasets and configs are YAML. JSON is not accepted as a primary format. (Internal serialization can use JSON; the user-facing surface is YAML.)

### F3. Pluggable system adapter
v0 ships with `http` and `python_function` adapters. The runner does not know which one is active.

### F4. Pluggable evaluator
v0 ships with three built-in evaluators: `contains_text`, `tool_called`, `llm_judge`. Custom evaluators register via entry-point.

### F5. Trace capture
Every case run produces a Trace object with a documented schema. Traces are written before evaluators run, so a partial run still produces inspectable artifacts.

### F6. Variant matrix
A single config can declare multiple `systems`. The runner expands `cases × systems` into a run matrix and stores results per variant. v0 supports endpoint variants (different URL or query params) without git checkout.

### F7. Local-file storage
v0 writes to `runs/<run_id>/` — `config.yaml`, `traces.jsonl`, `results.jsonl`, `summary.yaml`. SQLite/Postgres/Langfuse adapters land in v0.1 and beyond.

### F8. Async runner
The runner is `async def`. Per-variant concurrency is bounded by `run.max_concurrency`. Failures in one case do not abort the run.

### F9. Comparison summary
When `run.compare_systems: true`, the run summary includes per-variant pass-rate, latency, cost, and a per-evaluator breakdown.

## Non-functional requirements

### N1. Extensibility before features
Every feature lands as an adapter or evaluator. The runner does not grow `if/else` branches per system type.

### N2. Boring core
The runner module is < 300 lines. If it grows past that, something belongs in an adapter.

### N3. Portable traces
Trace JSON is documented, stable, and self-describing (carries `schema_version`). A trace written by v0 is readable by v1.

### N4. macOS first, no hard git dependency
The default workspace adapter snapshots the filesystem without git. Git becomes a workspace adapter, not a runtime requirement.

### N5. Determinism where possible
Same config + same dataset + same model → same `summary.yaml` keys, same shape. Numeric values may vary (LLM judges); the schema does not.

### N6. Failure isolation
One case timing out, raising, or returning malformed JSON must not crash the run. The trace records the error; the evaluator runs against whatever was captured; the summary marks the case as `errored`.

## Success metrics

For v0:
1. **Time-to-first-eval**: a new user, given the repo, ships their first running eval in under 30 minutes.
2. **Lines per new evaluator**: adding a new evaluator type is < 80 lines including tests.
3. **Lines per new system adapter**: adding a new system adapter is < 150 lines including tests.
4. **Trace stability**: zero schema changes to the Trace object between v0.0 and v0.3.

## Open questions

1. Do we ship a `cli` adapter in v0 (process spawning) or defer to v0.1? *Tentative: defer.*
2. Cost tracking — do we compute it from token counts using a config'd price table, or require the system to emit it? *Tentative: both. System emits if it can; runner computes from a price table otherwise.*
3. Multi-turn user simulator — own evaluator type, or part of the system adapter? *Tentative: part of the system adapter; the simulator is "still the system."*
4. Workspace adapter — required for v0 even when not used? *Tentative: optional. If `evaluators` reference filesystem, runner requires `workspace`.*

## Out of scope, explicitly

- Generating datasets from production traffic. That is a separate tool.
- Replaying traces. v0 stores traces; replay lands later.
- Real-time alerting on regressions. Compare runs offline.
- Human review queues. Add later as a `human_review` evaluator that emits "pending" results.

## Naming

- Project: **Eval Harness**
- Package: `eval_harness`
- CLI: `evalh`
- Config: `eval.yaml`
- Dataset: `cases.yaml`
- Run output: `runs/<run_id>/`
