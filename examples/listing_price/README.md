# Example: listing_price

> **This is a shape reference, not a runnable smoke test.**
> It shows what a realistic Eval Harness setup looks like — HTTP adapter, multiple variants, an LLM judge, observability hooks. To actually run it, you need your own agent service listening at the configured `endpoint`. **Adapt this file for your project; don't try to run it from a fresh checkout.**
>
> If you want a runnable smoke test (no external service required, just an `ANTHROPIC_API_KEY`), see [`examples/tiny_demo/`](../tiny_demo/).

The canonical Eval Harness sample. Other docs in this repo (`README.md`, `ConfigSchema.md`, `Variants.md`) link here for the realistic shape of `eval.yaml` and `cases.yaml`.

## Scenario

A real-estate agent answers questions about house listings. To answer correctly it must:

1. Call `get_listing_details` to fetch the listing's suburb and price.
2. Call `get_average_suburb_price` to fetch the suburb benchmark.
3. Produce an answer that compares the two.

This example evaluates two variants of the agent — `agent_main` (the current production prompt) and `agent_experimental` (a candidate prompt) — against the same dataset, in parallel.

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | The run config. Two variants, four evaluators, local-file output. |
| [`cases.yaml`](cases.yaml) | Three cases across three suburbs. |

## Run it

You need your own agent service running first. The example assumes:
- An HTTP endpoint at `http://localhost:8000/chat` that accepts the `request_template` shape and returns the `response_mapping` fields.
- Two routing variants (`variant=main`, `variant=experimental`) reachable from the same endpoint via the query param.
- Whatever your agent calls under the hood: model API, tool implementations, etc.

If you have one, set `AGENT_API_KEY` and run:

```bash
evalh run examples/listing_price/eval.yaml
```

Output lands in `runs/<timestamp>_listing_price_eval/`.

If you don't have an agent service to point at, run [`tiny_demo`](../tiny_demo/) instead — it ships its own self-contained agent (calls Claude directly, no HTTP service required). Needs `ANTHROPIC_API_KEY`.

## What the run produces

```
runs/2026-05-03T10-30-00_listing_price_eval/
  config.yaml          # exact config used (env vars masked)
  config_hash.txt
  traces.jsonl         # one Trace per (case × variant)
  results.jsonl        # one EvaluationResult per (case × variant × evaluator)
  summary.yaml         # per-variant pass-rate, latency, comparison
```

## Inspect the run

```bash
evalh inspect runs/2026-05-03T10-30-00_listing_price_eval --case listing_price_001
evalh compare runs/<run_a> runs/<run_b>
```

## What this example does and doesn't show

Shows: HTTP system adapter, multiple variants, three built-in evaluator types (`tool_called`, `contains_text`, `llm_judge`), `pass_criteria`, retry config, local-file output.

Doesn't show: filesystem-modifying agents (see `examples/coding_agent/` when it lands in v1), streaming systems, observability platform integration. Those have their own examples documented in [`Adapters.md`](../../docs/Adapters.md), [`Filesystem.md`](../../docs/Filesystem.md), and [`Observability.md`](../../docs/Observability.md).

## Field-by-field reference

For the full schema — every field, every type, every default — see [`ConfigSchema.md`](../../docs/ConfigSchema.md).
