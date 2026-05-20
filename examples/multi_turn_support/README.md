# Example: multi_turn_support

A multi-turn customer-support eval. The `user_simulator` SystemAdapter plays an LLM-driven user persona that drives the conversation; an inner `python_function` adapter plays the support bot under test. `llm_judge` grades the **full transcript** for conversational hygiene — no repeated asks, goal resolution within the available turns, no vague deferrals.

This is the canonical shape for "the harness drives multiple turns on the user's behalf."

> **Deviation from plan.md.** The plan envisioned per-case persona configuration via `expected.persona`. The current `user_simulator` adapter takes `user_persona_prompt` at variant init time, not per case, and `ExpectedBehavior` does not carry a `persona` field. The example expresses the persona axis at the **variant** level instead; each case carries `metadata.persona` to tag its intended natural-fit variant. Cases × variants still expand to a matrix, so cross-pair cells (a "frustrated" persona running an "efficient" case, etc.) stress-test the agent against the wrong user style. A follow-up bead can add per-case persona routing if there's demand.

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | Two `user_simulator` variants (one per persona) wrapping the same inner `python_function` agent; `llm_judge` with `nl_assertions` over the transcript. |
| [`cases.yaml`](cases.yaml) | Four synthetic mortgage-support scenarios. Each seeds the initial user turn and tags `metadata.persona`. |
| [`agent.py`](agent.py) | The support bot under test. One Claude call per assistant turn, conditioned on the running conversation. |

## Required environment

```bash
export ANTHROPIC_API_KEY=...
# or put it in examples/multi_turn_support/.env (gitignored)
```

Online-only. The simulator needs an LLM to play the user role, so a deterministic offline path would defeat the adapter's value-prop. For the always-runnable smoke test see [`examples/tiny_demo/`](../tiny_demo/).

Optional extras the example surfaces as `ConfigError` (sqlite-store pattern) rather than ImportError:
- `anthropic` (required at runtime — `pip install 'eval-harness[anthropic]'`).
- `python-dotenv` (optional convenience; the example falls back to plain `os.environ` if missing).

## Run it

```bash
evalh run examples/multi_turn_support/eval.yaml
```

Expected runtime: 1–2 minutes for the shipped 4 cases × 2 variants = 8 cells (each cell does up to 5 user turns + 5 agent turns + 1 judge call). Expected total cost: well under $0.50 with the configured `cost_limit_usd: 1.00` ceiling.

## What happens, in order

1. The runner expands `cases × variants` into 8 cells. Each variant pins a distinct `user_persona_prompt` (frustrated buyer vs efficient buyer).
2. For each cell, `user_simulator` enters: it appends `case.input.user_message` to a fresh conversation and calls the inner `python_function` agent with `input.conversation` carrying the transcript so far.
3. The agent ([`agent.py`](agent.py)) issues one Claude call per turn, conditioned on the full conversation. It returns a `final_answer`.
4. `user_simulator` appends the assistant reply to the transcript, then evaluates `stopping_criterion` (`content_match` on `"resolves it"` / `"all set"`). If not stopped and turns remain, it asks the user-LLM for the next user turn using `user_persona_prompt` as the system prompt and the rendered conversation as input.
5. The loop ends on stopping match or `max_turns=5`. The final `Trace.messages` carries the full alternating transcript; `Trace.extra.user_simulator` records `turns` and `stop_reason`.
6. `llm_judge` grades the trace. `include_in_prompt: [input.user_message, messages, output.final_answer]` surfaces the full transcript to the judge so per-turn behavior (repeated asks, deferrals) is gradable, not just the last reply.
7. `pass_criteria.all_required: [conversation_quality]` gates the cell. The per-variant summary in `runs/<id>/summary.yaml` answers "which persona is harder for the agent?".

## Why this works

`user_simulator` is the test scaffold; the agent is the system under test. Three things make multi-turn evaluation work here:

- **The transcript is the artifact.** The judge sees `trace.messages` — every user/assistant pair — not just `output.final_answer`. Assertions like "never asked for the same information twice" are only checkable against the transcript.
- **Persona is a variant axis.** Today the simulator persona is configured once at adapter init. Treating it as a variant lets the runner give you per-persona pass rates for free.
- **Stopping is bounded twice.** `stopping_criterion: content_match` lets the simulated user end naturally when the agent resolves the goal; `max_turns: 5` caps cost when it doesn't.

## Extending it

- Add cases to [`cases.yaml`](cases.yaml). Each case needs `input.user_message`; `metadata.persona` is informational.
- Add personas as new variants in [`eval.yaml`](eval.yaml) — copy a `systems:` entry and edit `user_persona_prompt`.
- Swap stopping behavior to `type: judge` with a `model:` and `question:` if `content_match` is too brittle (e.g., the simulated user phrases the resolution differently each run).
- Swap the inner agent for `adapter: http` to test a remote support service — `user_simulator` doesn't care what the inner adapter is, only that it returns a `Trace.output.final_answer`.
- Tighten the judge: add assertions to `nl_assertions`, or move from `pass_when: all` to `pass_when: k_of_n=2` if you want partial credit.
