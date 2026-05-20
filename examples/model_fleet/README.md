# Example: model_fleet

A three-model fleet eval. The same Melbourne real-estate QA prompt is shipped against Anthropic Haiku, Anthropic Sonnet (claude-4-7), and OpenAI GPT-5.5 — one config, three variants. The runner expands `cases × 3` and dispatches concurrently; `summary.yaml` shows pass-rate, cost, and latency per variant against the `anthropic-haiku` baseline so a reader can answer "which model wins on my domain?" at a glance.

This is the canonical shape for "I'm deciding between three models — which one wins?" It is also the first example of the run-level cost guardrail (`run.cost_limit_usd`) and the first example to push beyond the two-variant A/B form into a genuine fleet.

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | Three `http` variants (one per model), `llm_judge` + `cost_under` + `latency_under` evaluators, `baseline_variant: anthropic-haiku`, `cost_limit_usd: 0.50`. |
| [`cases.yaml`](cases.yaml) | Twenty Melbourne residential property QA cases — descriptive, comparison, advice, and knowledge questions across Richmond, Brunswick, Carlton and neighbours. Domain reuses [`examples/listing_price/`](../listing_price/). |

## Required environment

> **Online-only.** This example calls live LLM APIs. The always-runnable smoke path is [`examples/tiny_demo/`](../tiny_demo/eval.yaml) — run that first if you just want to verify your install.

```bash
export ANTHROPIC_API_KEY=...    # for anthropic-haiku, anthropic-sonnet
export OPENAI_API_KEY=...       # for openai-gpt-5-5
```

Both keys must be set — the config refuses to load if either is missing (env-var expansion happens at plan time, before any case runs).

CI does not exercise this example. The repo's smoke workflow only runs `tiny_demo`.

## Run it

```bash
evalh run examples/model_fleet/eval.yaml
```

Expected runtime: under 60s for 20 cases × 3 variants at `max_concurrency: 4`. Expected total spend: well under the configured `$0.50` ceiling on a clean run.

## What happens, in order

1. The runner expands `cases × variants` — 20 × 3 = 60 cells — and dispatches them through a semaphore of 4.
2. Each cell hits a different LLM endpoint. The two Anthropic variants use the `anthropic_messages` preset; the OpenAI variant uses `openai_chat`. The preset fills in `request_template` (a one-message user prompt) and `response_mapping` (final answer + token usage), so the YAML stays short and free of JSONPath.
3. The http adapter records `latency_ms`, `metrics.token_input`, `metrics.token_output`, and — via the LLM-backends pricing table — `metrics.cost_usd` into the trace.
4. Evaluators run against each trace:
   - `answer_quality` (`llm_judge` with three `nl_assertions`, judged by `claude-4-7`) — the quality bar is identical across variants so the comparison is honest.
   - `cost_per_case_under` — refuses any single call above `$0.02`.
   - `latency_per_case_under` — refuses any single call above 30s.
5. As cells complete, accumulated `metrics.cost_usd` is summed against `run.cost_limit_usd: 0.50`. If that ceiling is crossed mid-run, queued cells short-circuit with a `cost_limit` Trace — the run still produces a valid `summary.yaml`, it just stops paying for new dispatches.
6. `summary.yaml` includes a ComparisonReport block keyed by `baseline_variant: anthropic-haiku`, so Sonnet and GPT-5.5 show up as `vs baseline` deltas — pass-rate flips, mean latency delta, and mean cost delta per case.

## Why this works

Fleet evaluation is what the variant primitive was designed for. Variants in eval-harness are *parameterisations of one run*, not a hardcoded "champion vs challenger". Three variants is the same machinery as two — a fourth, fifth, or eighth model is one more entry in `systems[]` with no runner changes. The trace contract is what makes that legible: each variant emits the same trace shape, so the same evaluators score them on the same axes, and the ComparisonReport reads off the resulting `results.jsonl` without per-model branching.

The provider-preset shortcut keeps the configuration honest. The example does not invent an agent or a tool harness — the system under test is the LLM endpoint itself. `provider: anthropic_messages` and `provider: openai_chat` save you from authoring `request_template` and `response_mapping` for well-known APIs; the preset is a saved shape, not a new code path. If a provider's response shape changes, the preset is the single place that updates.

The run-level cost ceiling matters because a fleet eval is the most likely place for a model swap to blow a budget. `run.cost_limit_usd: 0.50` is a soft cap on the accumulated `trace.metrics.cost_usd` across completed cells; once it's crossed, dispatch stops and the run still produces a clean `summary.yaml`. The per-evaluator `cost_limit_usd` on `llm_judge` is independent and additive — it caps any single judge call from running away on a very long answer. The two guardrails are layered: one bounds the run, the other bounds the call.

## Extending it

- **Add a fourth model**: append a new entry to `systems[]` — say a `provider: anthropic_messages` variant on a different Claude model or a `provider: openai_chat` variant on a smaller GPT. The runner expands `cases × 4` automatically; `summary.yaml` grows a column.
- **Tighten the cost ceiling**: drop `run.cost_limit_usd` to `0.20`. Slow, expensive cells will be sacrificed first, and the report will show which variant tripped the cap.
- **Compare prompts, not models**: copy the file, point all three variants at the same model, and vary `body.system` (or the request template) per variant. Same machinery, different axis.
- **Promote a new baseline**: change `baseline_variant` to the model that won last time. The `vs baseline` columns now treat that model as the bar to beat; regressions show up as negative deltas.
- **Anchor a regression gate**: feed this example's `summary.yaml` into the `regression_gate` example to enforce that no variant slips below its prior pass-rate on PR.
