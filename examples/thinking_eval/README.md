# Example: thinking_eval

An extended-thinking eval. The same Claude variant is shipped against two contrasting cases — a logic puzzle that should benefit from extended thinking and a one-word factual lookup that shouldn't. The same three thinking evaluators score both, so a single run answers both *"is my agent leaking its internal reasoning?"* and *"is the thinking proportionate to the problem?"*

This is the canonical shape for *"I'm using Claude's extended thinking. Did my agent leak its internal reasoning, and was the thinking proportionate?"* It is the first and only example that anchors the entire thinking-evaluator family (`thinking_present`, `thinking_tokens_under`, `thinking_does_not_leak`).

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | One `http` variant with `provider: anthropic_messages` and `body.thinking: {type: enabled, budget_tokens: 2048}`. Wires all three thinking evaluators. |
| [`cases.yaml`](cases.yaml) | Two cases: `tk_logic_puzzle` (non-trivial, expects thinking) and `tk_trivial_capital` (trivial, expects near-zero thinking). |

## Required environment

> **Online-only.** This example calls the live Anthropic Messages API. The always-runnable smoke path is [`examples/tiny_demo/`](../tiny_demo/eval.yaml) — run that first if you just want to verify your install.

```bash
pip install 'eval-harness[anthropic]'    # required: the leak-judge needs the anthropic SDK
export ANTHROPIC_API_KEY=...              # system under test + the leak-judge
```

The `anthropic` extra is needed even though the system under test is reached over plain HTTP — the `thinking_does_not_leak` evaluator's judge resolves to the Anthropic backend at plan time. The backend's import guard raises `ConfigError` with an install hint if the extra is missing (mirrors `sqlite_store.py`), so a missing extra fails before any case dispatches.

CI does not exercise this example. The repo's smoke workflow only runs `tiny_demo`.

## Run it

```bash
evalh run examples/thinking_eval/eval.yaml
```

Expected runtime: under 60s for two cases at `max_concurrency: 2`. Expected spend: a few cents — well under the per-judge-call `cost_limit_usd: 0.05`.

## What happens, in order

1. The runner expands `cases × variants` — 2 × 1 = 2 cells — and dispatches them through a semaphore of 2.
2. Each cell POSTs to the Anthropic Messages endpoint. The `anthropic_messages` preset fills in `request_template` (one user message) and `response_mapping` (final answer, thinking, token counts including `usage.thinking_tokens`), so the YAML carries only the thinking-specific surface: the `thinking` block in `body` and the budget figure.
3. The http adapter records `output.final_answer`, `output.thinking`, `metrics.token_thinking`, plus the standard `latency_ms` / `token_input` / `token_output` into the trace. Trace shape is unchanged — thinking is already first-class in the schema.
4. Evaluators run against each trace:
   - `thinking_present` — sanity check that `output.thinking` is a non-empty string. **Informational** in this example: the trivial case may legitimately emit near-zero thinking, so gating on presence would punish a well-behaved model.
   - `thinking_tokens_under` (`max_tokens: 3000`) — refuses any case whose `metrics.token_thinking` lands above the configured budget headroom. The `body.thinking.budget_tokens` is `2048`; `3000` is the alarm point for a runaway. **Gating.**
   - `thinking_does_not_leak` — calls a small Claude judge in `judge_assertions` mode with two natural-language assertions and a JSON schema. **Gating.**
5. As cells complete, the trace, results, and a `summary.yaml` land under `runs/<run_id>/`. The summary's per-variant block shows pass-rate across the two cases and mean `metrics.token_thinking` — that's how a reviewer notices the trivial case is silently spending its budget.

## Why this works

Extended thinking is a Claude-specific feature that has been first-class in the trace schema since v0 but had no narrative example until now. The single-variant shape is deliberate: thinking eval is about *one* model's behaviour on *contrasting* inputs, not a horse race between models — that story lives in [`model_fleet/`](../model_fleet/). Two cases is the floor that lets the same evaluator suite say something interesting about both budget bloat and leakage from one run; adding a fifth or tenth case is one more line in `cases.yaml`.

Splitting the three evaluators along the *gating / informational* axis is the lesson the Tier-1 examples learned. `thinking_present` would be a false negative on the trivial case if it were gating; `thinking_tokens_under` and `thinking_does_not_leak` are real production gates that should fail loudly. The eval.yaml comment marks the informational evaluator explicitly so a reader scanning the config knows which lights are decorative and which are the alarm.

The leak check uses `judge_assertions` mode rather than a literal-string `forbidden_patterns` list because the model's hidden chain of thought varies in phrasing run-to-run. A semantic assertion (*"the answer does not say 'let me think'"*) catches paraphrases that a substring match would miss; the JSON schema constrains the judge to a `{leaks, reason}` object so the evaluator's verdict is mechanically derivable.

## Extending it

- **Add another non-trivial case**: append a multi-step reasoning task (e.g. a small constraint-satisfaction problem) to `cases.yaml`. The eval matrix grows by one cell; the evaluators score it identically.
- **Tighten the thinking budget**: drop `body.thinking.budget_tokens` and `thinking_tokens_under.max_tokens` together. The puzzle case is where the headroom matters; the trivial case will sit comfortably under any sane ceiling.
- **Swap models**: change `body.model` from `claude-opus-4-7` to another thinking-capable Claude model (`claude-4-7`, `claude-sonnet-4-6`). The preset's `response_mapping` is provider-shaped, not model-shaped, so the YAML doesn't change.
- **Add a forbidden-pattern check**: layer a literal-string check on top of the semantic one by adding `forbidden_patterns: ["chain of thought", "internal reasoning"]` to the same evaluator's config — the evaluator runs both checks and ORs the verdict.
- **Compare against thinking off**: copy the variant block, drop the `thinking` field, and run as a two-variant fleet — the resulting `summary.yaml` shows what the same evaluators say when the feature is disabled.
