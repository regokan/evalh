# Example: observability_langfuse

Three-pattern integration with Langfuse, runnable offline by default. Demonstrates the only three roles a platform plays in this harness:

1. **DatasetAdapter** — pull production traces from Langfuse as eval cases.
2. **TraceStore** — mirror local eval runs to the Langfuse UI.
3. **TraceEnricher** — fold an upstream Langfuse span onto the local Trace per cell.

The point: platforms are sources, sinks, and enrichers — never the source of truth. The canonical artifact is `runs/<run_id>/traces.jsonl` on local disk.

> **Deviation from plan.md.** The plan called for the langfuse store to "no-op with a warning when `LANGFUSE_API_KEY` is unset" and to ship in the default `output:` list. The actual `langfuse_trace_store.py` / `langfuse_enricher.py` raise `ConfigError` at plan time when the `[langfuse]` extra isn't installed — there is no silent no-op path. So in this example the **langfuse store and enricher are commented out by default**, with explicit uncomment instructions in this README. The langfuse **dataset** is similarly commented (consistent presentation, even though only the active dataset is instantiated). Same shape, same demo — just honest about when each block tries to import the SDK.
>
> Secondary deviation: the bead spec lists `fixtures/dataset.jsonl` as the offline default. The `fixture` DatasetAdapter is YAML-only, so the active offline path uses `cases.yaml` via `dataset.type: fixture`. `fixtures/dataset.jsonl` is shipped as a parallel reference — swap to `dataset.type: jsonl, path: examples/observability_langfuse/fixtures/dataset.jsonl` to use it instead.

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | Fixture dataset + local_files store by default. Three commented blocks (`dataset.type: langfuse`, `output[].type: langfuse`, `systems[].enrich_trace_from`) hold the online swap. |
| [`cases.yaml`](cases.yaml) | Three synthetic support-bot cases (rate lock, appraisal, PMI). Loaded by the default `dataset.type: fixture`. |
| [`agent.py`](agent.py) | Deterministic async support bot — classifies the message, returns a canned reply, and stamps `trace.extra.trace_id` so the enricher has something to look up when activated. |
| [`fixtures/dataset.jsonl`](fixtures/dataset.jsonl) | Same cases in JSONL form. Use via `dataset.type: jsonl, path: ...`. |

## Required environment

None for the offline default. Run it on a fresh checkout, no keys.

Optional extras (each activates one pillar of the triplet):

| Pillar | Extra | Env vars | Behavior if unset |
|---|---|---|---|
| Dataset | `pip install 'eval-harness[langfuse]'` | `LANGFUSE_API_KEY`, `LANGFUSE_HOST` | `ConfigError` at plan time if the langfuse dataset stanza is uncommented and SDK is missing. |
| Store | `pip install 'eval-harness[langfuse]'` | `LANGFUSE_API_KEY`, `LANGFUSE_HOST` | `ConfigError` at plan time if the langfuse store entry is uncommented and SDK is missing. |
| Enricher | `pip install 'eval-harness[langfuse]'` | `LANGFUSE_API_KEY`, `LANGFUSE_HOST` | `ConfigError` at plan time if `enrich_trace_from` is uncommented and SDK is missing. |

All three follow the `sqlite_store.py` ConfigError-on-missing-extra pattern.

## Run it

```bash
evalh run examples/observability_langfuse/eval.yaml
```

Expected runtime: under a second. The agent is deterministic; the three cases pass `answer_mentions_topic`; nothing touches the network.

## What happens, in order

1. The `fixture` dataset adapter reads `cases.yaml` and returns three `EvalCase`s.
2. The `python_function` system adapter calls `agent.run(case, variant)`. The agent matches the user message against a tiny keyword table and returns `final_answer`, a synthetic `tool_call`, and an `extra.trace_id`.
3. The runner composes a `Trace` per cell and hands it to the `local_files` store, which writes `runs/<run_id>/traces.jsonl` and `results.jsonl`.
4. Evaluators score each cell. `answer_mentions_topic` gates the cell; `under_two_seconds` is informational.
5. (Commented in default config.) The langfuse store mirrors each trace to your Langfuse project. The langfuse enricher fetches the upstream span by `trace.extra.trace_id` and merges the `merge:` spec onto the local Trace.

## The three patterns, side by side

```yaml
# 1. DATASET — pull from Langfuse instead of a local fixture.
dataset:
  type: langfuse
  api_key: ${LANGFUSE_API_KEY}
  host: ${LANGFUSE_HOST}
  filter: { tags: [production], timestamp_gt: "2026-04-26T00:00:00Z" }
  sample: 50
  embed_full_trace: true   # pair with `adapter: replay` for offline backtesting of prod traffic

# 2. STORE — mirror local runs to the Langfuse UI.
output:
  - type: local_files
    path: runs/
  - type: langfuse
    api_key: ${LANGFUSE_API_KEY}
    host: ${LANGFUSE_HOST}

# 3. ENRICHER — fold the upstream Langfuse span onto the local Trace.
systems:
  - name: support_bot
    adapter: python_function
    target: examples.observability_langfuse.agent:run
    enrich_trace_from:
      - type: langfuse
        api_key: ${LANGFUSE_API_KEY}
        host: ${LANGFUSE_HOST}
        wait_for_ingestion_seconds: 2.0
        merge:
          "metrics.token_input":  "$.usage.input_tokens"
          "metrics.token_output": "$.usage.output_tokens"
          "extra.upstream_score": "$.score"
```

## Why this works

- **Canonical sink is local.** Every store writes the same `Trace` model; the `local_files` sink is always on. Langfuse is a mirror — kill it without losing the run.
- **Sources don't blur with sinks.** A Langfuse dataset reads from Langfuse; a Langfuse store writes to Langfuse; a Langfuse enricher fetches one upstream span per cell. Same SDK, three roles, no shared mutable state in adapter land.
- **Failure-soft enrichment.** Per `docs/Adapters.md`, the enricher *raises* on miss; the runner records the failure in `trace.extra.enrichment_errors` and continues. Production-observability hiccups stay out of pass/fail.

## Extending it

- Add cases to [`cases.yaml`](cases.yaml) (or to [`fixtures/dataset.jsonl`](fixtures/dataset.jsonl) if you prefer the JSONL form).
- Add a second platform pillar — Phoenix and Arize ship with the same triplet shape. Swap `type: langfuse` → `type: phoenix` / `type: arize` in any of the three blocks.
- Tighten the `merge:` spec to fold richer upstream fields (cost, model, span tree, user_score) onto every cell.
- Pair `dataset.type: langfuse` + `embed_full_trace: true` + `adapter: replay` for the full backtest-prod-offline pattern; the [`examples/online_eval/`](../online_eval/) example shows the replay side in isolation.
