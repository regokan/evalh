# Example: online_eval

Online evaluation against historical traces. The dataset is fetched from an observability platform (Langfuse here); the `production_replay` variant scores those traces without invoking any system; an optional `candidate_v4` variant backtests a new prompt against the same inputs.

This is the workflow described in [Observability.md → Pattern 4](../../docs/Observability.md#pattern-4-online-evaluation).

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | The run config. Langfuse dataset, replay + candidate variants, three evaluators. |

This example does not ship a `cases.yaml` — cases come from Langfuse at run time.

## Required environment

```bash
export LANGFUSE_API_KEY=...
export LANGFUSE_HOST=https://cloud.langfuse.com
export AGENT_API_KEY=...                  # only needed if you keep candidate_v4
```

## Run it

```bash
# To score production only — comment out `candidate_v4` in eval.yaml first
evalh run examples/online_eval/eval.yaml
```

## What you get

Per-case results for production traffic, plus (if `candidate_v4` is enabled) a per-case comparison: which cases the candidate would have flipped to passing, and which it would have regressed.

```
runs/<run_id>/summary.yaml   # comparison.deltas[].regressions / .improvements
```

## Three modes summarised

| Mode | Source of trace | System invoked? | Configured by |
|---|---|---|---|
| **Offline eval** (curated) | `cases.yaml` you wrote | yes | `dataset.type: yaml` + HTTP/fn/CLI variants |
| **Backtesting** | Production traces (input only) | yes — candidate runs against historical inputs | `dataset.embed_full_trace: false` + HTTP variant(s) |
| **Online eval** (this example) | Production traces (full) | **no** | `dataset.embed_full_trace: true` + `adapter: replay` |

All three use the same runner, evaluators, trace store, and comparison report.
