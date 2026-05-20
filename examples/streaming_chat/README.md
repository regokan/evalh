# Example: streaming_chat

A streaming chat-agent eval. The agent emits a canned answer token-by-token and records the three streaming metrics the harness ships dedicated evaluators for — time-to-first-token, tokens/sec, and stream-completion. Two variants run the same agent under different simulated latency conditions: a cold-start `streaming-default` and a fast `streaming-warm-cache`. The run is scored against two latency budgets — a relaxed one that both variants clear and a tighter one only the warm-cache variant clears.

This is the canonical shape for "my chat endpoint streams — is it fast enough to ship?" and the first example of the three streaming-only evaluators (`latency_first_token_under`, `tokens_per_second_above`, `stream_completed`). It also shows the *variants-as-budget-profiles* pattern: variants need not be A/B; they can model different deployment conditions of the same agent.

## Files

| File | What it is |
|---|---|
| [`agent.py`](agent.py) | The agent — async generator over a canned answer. Synthesises the streaming metrics from variant metadata. ~70 lines. |
| [`cases.yaml`](cases.yaml) | Three short chat prompts. Each carries the entity the answer must mention. |
| [`eval.yaml`](eval.yaml) | Wires `python_function` agent + two variants (budget profiles) + the three streaming evaluators + `contains_text`. |

## Required environment

None — this example is offline by design. No API key, no network, no third-party SDK.

```bash
pip install eval-harness
# or with uv:  uv add eval-harness
```

## Run it

```bash
evalh run examples/streaming_chat/eval.yaml
```

Expected runtime: under 1s for the three shipped cases across both variants.

## What happens, in order

1. The runner expands `cases × variants` — three cases, two variants — into six cells.
2. For each cell the `python_function` adapter calls `agent.run(case, variant)`.
3. The agent iterates the canned answer token-by-token (cooperative `await asyncio.sleep(0)`), then returns a result dict whose `metrics` carry the streaming fields read from the variant's `metadata.simulated_ttft_ms` and `metadata.simulated_tps`. In a real chat agent these would be `time.perf_counter()` measurements around the actual SSE stream — the harness contract is just "put a number in the trace."
4. Evaluators run against the trace:
   - `latency_first_token_under` (relaxed, `max_ms: 800`) — both variants clear it.
   - `latency_first_token_under` (tight, `max_ms: 300`) — only `streaming-warm-cache` clears it.
   - `tokens_per_second_above` (`min_tps: 30`) — both variants clear it.
   - `stream_completed` — guards against a silently truncated stream.
   - `contains_text` — defaults its `all_of` to `case.expected.answer_should_include` and checks the answer text.
5. `pass_criteria.all_required` gates on the *relaxed* budget plus the throughput / stream / answer checks. The tighter warm-cache budget runs but isn't required — its per-variant pass rate is the signal that surfaces in `summary.yaml`.
6. Results land in `runs/<run_id>/{config.yaml,traces.jsonl,results.jsonl,summary.yaml}`.

## Why this works

A streaming chat agent has a small number of metrics that decide whether it ships: time-to-first-token (the user's first impression), sustained throughput (whether the answer streams smoothly), and stream completion (whether the connection actually finished). The three streaming evaluators read those three numbers directly off the trace — no special harness mode is needed, no separate "streaming runner", no SSE-specific adapter. The agent just records the metrics it would have logged anyway.

The two-variant shape demonstrates that variants are not just A/B comparisons of two systems. They are *parameterisations of one system under different conditions*. The same agent code runs under cold-start and warm-cache profiles; the run output shows which budgets each profile clears. Adding a third profile (`streaming-saturated`, `streaming-edge-pop`, …) is one more entry in `systems[]` — the runner expands the matrix automatically.

Recording the metric values directly from a synthetic source (here, variant metadata) makes the example *deterministic and offline*. The evaluators don't care whether the number came from a real stream or a fixture — they just read the trace. Swap the canned answer for a real Anthropic streaming call and the same evaluators continue to fire against the same trace fields.

## Extending it

- **Add cases**: append to `cases.yaml` and add the matching canned answer to `_CANNED_ANSWERS` in [`agent.py`](agent.py). Each case needs `expected.answer_should_include` so the `contains_text` evaluator has something to check.
- **Add a third budget profile**: append a new entry to `systems[]` with different `simulated_ttft_ms` / `simulated_tps` metadata. The runner expands `cases × 3` automatically.
- **Tighten the gate**: move `ttft_under_warm_cache_budget` into `pass_criteria.all_required`. The default variant will then fail the run — exactly the regression-detection behaviour you want when promoting warm-cache as the baseline.
- **Real streaming**: swap the canned answer for an `anthropic.AsyncAnthropic` streaming call (`client.messages.stream(...)`). Measure `time.perf_counter()` at the start, again on the first chunk, again on the last; populate the same three metric fields. The evaluators don't change.
