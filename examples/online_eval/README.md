# Example: online_eval

Online evaluation against historical traces — runnable **offline**.

The dataset here is a YAML fixture (`embedded_traces.yaml`) standing in for what a real observability platform (Langfuse, Phoenix, Arize, OTel) would return. The `production_replay` variant scores those traces without invoking any system. To run it against a live platform, swap the `dataset.type` block — everything else is unchanged.

This is the workflow described in [Observability.md → Pattern 4](../../docs/Observability.md#pattern-4-online-evaluation).

## Files

| File | What it is |
|---|---|
| [`embedded_traces.yaml`](embedded_traces.yaml) | Three synthesized 'production' traces with `embedded_trace` payloads (shape matches Langfuse-style output). |
| [`eval.yaml`](eval.yaml) | `dataset.type: fixture` + `embed_full_trace: true` + a `replay` variant + three evaluators. |

There is no separate `cases.yaml` — the `fixture` DatasetAdapter reads cases and their embedded traces from the single file above. With a real platform adapter (`langfuse`, `phoenix`, ...) cases come from the platform at run time.

## Run it

```bash
evalh run examples/online_eval/eval.yaml
```

No environment variables, no network. The runner produces a `runs/<run_id>/` directory with:

- `traces.jsonl` — the replayed traces (`extra.source == "replay"`, `extra.replayed_from.platform == "fixture"`, original `started_at` / `finished_at` / `latency_ms` / `metrics.*` preserved byte-for-byte).
- `results.jsonl` — per-evaluator verdicts.
- `summary.yaml` — per-variant aggregate.

Expected outcome on the shipped fixture: **2/3 cases pass.** `prod_003` is intentionally a weak trace — the agent skipped the `get_average_suburb_price` tool — so the evaluators have something to fail on.

## Going from fixture to a real platform

Replace the `dataset:` block in `eval.yaml`:

```yaml
dataset:
  type: langfuse                  # phoenix, arize, otel, ... when shipped
  api_key: ${LANGFUSE_API_KEY}
  host: ${LANGFUSE_HOST}
  filter:
    tags: [production]
    timestamp_gt: "2026-04-26T00:00:00Z"
    user_score_lt: 0.5
  sample: 100
  embed_full_trace: true
```

Everything else — the `replay` variant, the evaluators, the trace store — stays identical. The `embed_full_trace: true` flag is the contract the `replay` adapter cares about; how the cases were loaded is opaque to it.

## Backtest a candidate against the same inputs

Uncomment the `candidate_v4` block in [`eval.yaml`](eval.yaml) and start your service on `http://localhost:8000/chat` (set `AGENT_API_KEY` if it expects auth). The runner will expand `cases × variants`: each case gets one replayed cell and one fresh `candidate_v4` cell, with a per-case comparison in `summary.yaml`.

## Three modes summarised

| Mode | Source of trace | System invoked? | Configured by |
|---|---|---|---|
| **Offline eval** (curated) | `cases.yaml` you wrote | yes | `dataset.type: yaml` + HTTP/fn/CLI variants |
| **Backtesting** | Production traces (input only) | yes — candidate runs against historical inputs | `dataset.embed_full_trace: false` + HTTP variant(s) |
| **Online eval** (this example) | Production traces (full) | **no** | `dataset.embed_full_trace: true` + `adapter: replay` |

All three use the same runner, evaluators, trace store, and comparison report.
