# Example: tiny_demo

Self-contained Eval Harness smoke test. Self-contained at the *infrastructure* level — no DB to spin up, no separate HTTP service, no fixtures. The agent calls Claude directly, the judge calls Claude directly. **Real LLM, real stochasticity** — that's what Eval Harness exists to evaluate. A deterministic stub here would only test plumbing, not evaluation.

This is the example the test suite uses for end-to-end validation. **Don't model your real evals on this** — model them on [`listing_price/`](../listing_price/), which shows realistic shape (HTTP adapter, multiple variants, observability hooks). Use this one to sanity-check that Eval Harness is installed correctly and works end-to-end against a real model.

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | Run config. Two variants (`agent_concise`, `agent_verbose`) both pointing at the same agent callable via `python_function`. |
| [`cases.yaml`](cases.yaml) | Three cases across three suburbs. |
| [`agent.py`](agent.py) | An async callable that calls Claude with two tools (`get_listing_details`, `get_average_suburb_price`) backed by a hardcoded dict. ~120 lines. |

## Run it

```bash
# Install eval-harness with the Anthropic backend (needed for the agent + the judge)
pip install 'eval-harness[anthropic]'
# or with poetry:  poetry install --extras anthropic

export ANTHROPIC_API_KEY=...
evalh run examples/tiny_demo/eval.yaml
```

Output: `runs/<run_id>/{config.yaml,traces.jsonl,results.jsonl,summary.yaml}`.

Default agent model: `claude-haiku-4-5-20251001` (cheap). The smoke test should cost a few cents per full run (3 cases × 2 variants × tool turns + 6 judge calls).

The `anthropic` extra installs the `anthropic` Python SDK. eval-harness's core does not depend on any LLM provider — install only the backends you use.

## Why this exists

[`examples/listing_price/`](../listing_price/) is the *realistic shape*: an HTTP agent service, observability hooks, real LLM judge. Adapting it for your team is the right move — but it requires a real agent listening at `http://localhost:8000/chat`, which makes it useless as a CI smoke test.

`tiny_demo/` fills that gap. The runner, dataset adapter, evaluators, factories, registries, and trace store all exercise end-to-end against a real LLM. The only thing not exercised is the network path of the HTTP adapter — that's covered by unit tests with mocked transport.

## Why not deterministic?

You might ask: why not have `agent.py` return a canned answer so the test runs offline?

Because Eval Harness is for evaluating *stochastic* systems. The point of `llm_judge` (and the rest of the harness) is to produce a stable result on top of an unstable substrate. A deterministic agent would only test that the harness's plumbing works — not that judges actually judge, that aggregations actually aggregate, or that the comparison report actually catches real variance between variants.

Plumbing is what unit tests are for. They live in `tests/` and use mocks. This example is for the layer above.

## Contract: what `python_function` expects from the target callable

The target is an `async def` that takes a case (and optionally a variant) and returns a dict shaped like a Trace's output fields:

```python
async def run(case: dict, variant: dict | None = None) -> dict:
    return {
        "final_answer": str,             # required
        "thinking":     str | None,      # optional, captured into Trace.output.thinking
        "tool_calls":   list[dict],      # optional; each {"name", "arguments"}
        "tokens": {                      # optional
            "input":    int,
            "output":   int,
            "thinking": int,
        },
    }
```

The `python_function` adapter wraps this into a `Trace` (see [`DataModel.md`](../../docs/DataModel.md)). Latency, timestamps, error handling, and trace persistence are all handled by the adapter — your callable just produces the content.

See [`Adapters.md > python_function`](../../docs/Adapters.md#v0-python_function) for the full contract.

## Use as a CI smoke test

```yaml
# .github/workflows/eval-smoke.yml
env:
  ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
steps:
  - run: poetry install --extras anthropic
  - run: poetry run evalh run examples/tiny_demo/eval.yaml
  - run: test -f runs/*/summary.yaml
```

If you want a CI smoke test that doesn't spend money or call the network, write a unit-test-level integration test using mocks under `tests/integration/` — see the test suite for examples. The `tiny_demo/` example is for the real-LLM smoke test specifically.
