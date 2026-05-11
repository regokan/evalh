# Architecture rules — deltas

The design lives in `docs/`. This file is the *gotcha list*.

## Anti-patterns to refuse

| If you're about to write… | Do this instead |
|---|---|
| `if config.system.adapter == "http"` inside `eval_harness/runner/` | Push to a factory in `eval_harness/factories/` |
| `httpx.AsyncClient(...)` inside `runner/` | Push to an adapter in `eval_harness/adapters/` |
| `if "richmond" in trace.output.final_answer` inside `runner/` | Push to an evaluator in `eval_harness/evaluators/` |
| `for case in cases:` synchronous loop in the hot path | `asyncio.gather` + `Semaphore` from `run.max_concurrency` |
| `time.sleep(x)` anywhere | `await asyncio.sleep(x)` |
| `import anthropic` inside `eval_harness/` core | Move behind `[anthropic]` extra; dispatch in `llm_judge.py`'s backend registry |
| Adding a 7th adapter family | Stop. Update the docs first; come back. |
| Concatenating `output.thinking` into `output.final_answer` | Two fields, always separate. `metrics.token_thinking` is also separate. |
| Reading JSON for `eval.yaml` / `cases.yaml` | YAML only for human-authored files. JSONL only for machine artifacts. |
| Hard-requiring git for filesystem evals | Default is `tempdir_snapshot`; `git` is opt-in via the `[git]` extra. |
| Adding a side channel ("pass this from adapter to evaluator out of band") | Add it to the trace schema. See `docs/DataModel.md`. |

## The runner-stays-boring test

Before merging a PR that touches `eval_harness/runner/`, grep:
```bash
rg 'if .*adapter|if .*type|import (httpx|requests|anthropic|openai|git|yaml)' eval_harness/runner/
```
Should return nothing. If it does, the change belongs elsewhere.

## Schema bumps

The schema-bump checklist lives at [`docs/DataModel.md → Schema versioning`](../../docs/DataModel.md#schema-versioning). Use it.
