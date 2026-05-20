# Example: observability_langfuse

The Langfuse triplet — DatasetAdapter, TraceStore, TraceEnricher — in a single offline-runnable config.

> **Deviation from `examples/plan.md`** — plan.md lists `fixtures/dataset.jsonl` as the "synthetic offline dataset for fixture mode," but the `fixture` DatasetAdapter loads YAML (with embedded-trace shape) — not JSONL. We ship `cases.yaml` as the file the harness actually reads (per the bead's spec defaulting `dataset.type: fixture`), and keep `fixtures/dataset.jsonl` as a reference snippet of the production-Langfuse export shape so the README can point at it when explaining how `dataset.type: langfuse` would behave.

The point of this example is to demonstrate that platforms are *sinks, sources, and enrichers* — never the source of truth. The local `runs/<run_id>/` directory stays canonical; Langfuse is a mirror at most.

## Files

| File | What it is |
|---|---|
| [`agent.py`](agent.py) | Deterministic stub (no API key). Returns a canned answer with two tool calls so the evaluators have something to grade. |
| [`cases.yaml`](cases.yaml) | Three offline cases — what the `fixture` DatasetAdapter loads by default. |
| [`eval.yaml`](eval.yaml) | Wires the triplet: `dataset.type: fixture` + `output: [local_files, langfuse]` + commented `enrich_trace_from: [{type: langfuse}]`. |
| [`fixtures/dataset.jsonl`](fixtures/dataset.jsonl) | Reference: one line per upstream trace in the shape that `dataset.type: langfuse` would return. Not loaded by the run — it's documentation. |

## Required environment

All variables are **optional**. The example runs offline with none of them set.

```bash
# Optional — set to mirror traces to your Langfuse instance.
export LANGFUSE_API_KEY=...
export LANGFUSE_HOST=https://cloud.langfuse.com
```

Optional install extras (skip both for the offline path):

- `pip install 'eval-harness[langfuse]'` — only needed when `LANGFUSE_API_KEY` is set. Without the extra and without the key, the langfuse sink no-ops cleanly.

## Run it

```bash
evalh run examples/observability_langfuse/eval.yaml
```

Expected runtime: under five seconds for the three shipped cases (deterministic, no network).

## What happens, in order

1. The `fixture` DatasetAdapter reads [`cases.yaml`](cases.yaml) and produces three `EvalCase`s.
2. The `python_function` SystemAdapter calls [`agent.py`'s `run()`](agent.py) for each case. The stub returns a canned answer with two tool calls and a `trace_id` echoed from the case metadata.
3. The runner writes the resulting `Trace` to **both** sinks listed under `output:`:
   - `local_files` → `runs/<run_id>/traces.jsonl` (canonical).
   - `langfuse` → pushes to Langfuse if `LANGFUSE_API_KEY` is set; otherwise logs one warning per run and no-ops.
4. Evaluators (`tool_called`, `contains_text`) read the trace and grade pass/fail.
5. Results land in `runs/<run_id>/results.jsonl`; the per-variant summary in `runs/<run_id>/summary.yaml`.

## The three patterns, side-by-side

| Pattern | Direction | YAML key | When it fires | Default in this example |
|---|---|---|---|---|
| **DatasetAdapter** | platform → harness | `dataset.type` | plan time, loads cases | `fixture` (offline). `langfuse` block in eval.yaml is commented; uncomment to pull production traffic. |
| **TraceStore** | harness → platform | `output[].type` | runner writes each Trace | `local_files` + `langfuse`. The langfuse sink no-ops with a warning when `LANGFUSE_API_KEY` is unset, so the same config is correct offline and online. |
| **TraceEnricher** | platform → harness | `systems[].enrich_trace_from[].type` | per cell, after the system adapter, before evaluators | **Commented out by default.** Uncomment once `LANGFUSE_API_KEY` is set and your system emits real Langfuse trace ids. Failure-soft — ingestion lag goes into `trace.extra.enrichment_errors`, the cell still scores. |

## Going from offline to a live Langfuse instance

The same `eval.yaml` works against a real Langfuse with two small edits, no other downstream changes:

1. Swap the dataset block — comment `dataset.type: fixture`, uncomment `dataset.type: langfuse`.
2. Uncomment the `enrich_trace_from:` block under the system.

Set `LANGFUSE_API_KEY` (and optionally `LANGFUSE_HOST`) and install the extra:

```bash
export LANGFUSE_API_KEY=...
export LANGFUSE_HOST=https://cloud.langfuse.com
pip install 'eval-harness[langfuse]'
evalh run examples/observability_langfuse/eval.yaml
```

The runner pulls production traces as cases, mirrors each run back to Langfuse as observations + scores, and folds the upstream rich span into every local Trace before the evaluators see it.

## Why this works as a wiring demo

This example is the smallest possible config that exercises **all three** Langfuse touch-points in one file. If any of the three wires drifts — adapter registration, env-var expansion, the no-op fallback on the sink, the failure-soft contract on the enricher — this run breaks in a way the others don't catch.

It is also the canonical demonstration of `docs/Observability.md`'s posture: the harness owns the trace. The platform integration is a list of *pluggable mirrors*, not the system of record.

## Extending it

- Add cases by appending to [`cases.yaml`](cases.yaml). Provide `metadata.upstream_trace_id` so the (commented) enricher has a real id to fetch.
- Add a second canonical sink (e.g. `sqlite` or `postgres`) by appending to `output:` — the first entry stays canonical; the rest are mirrors.
- Swap `langfuse` for `phoenix` / `arize` / `otel`: each ships the same three-pattern shape, only the type names change.
