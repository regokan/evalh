# Examples

Fifteen runnable references — each one demonstrates a single, recognisable shape of AI-system evaluation. Pick the one that matches what you're trying to evaluate.

## Find your use-case

### Start here
| Example | Why it's the first stop |
|---|---|
| [`tiny_demo/`](tiny_demo/) | Install smoke test. Real Claude call, two variants, three evaluators. Finishes in under a minute. |

### Common eval shapes
| Example | What it evaluates | Anchor features |
|---|---|---|
| [`rag_qa/`](rag_qa/) | Retrieval-augmented QA — recall, answer quality, faithfulness, semantic similarity to a reference | `semantic_similarity`, hand-rolled retriever as a tool |
| [`streaming_chat/`](streaming_chat/) | Streaming agent latency — time-to-first-token, throughput, completion | `latency_first_token_under`, `tokens_per_second_above`, `stream_completed` |
| [`text_to_sql/`](text_to_sql/) | Agent emits SQL → execute against fixture DB → diff result | `command` evaluator, `schema_match`, `tempdir_snapshot` workspace |
| [`structured_extraction/`](structured_extraction/) | Agent emits JSON → enforce shape and per-field correctness | `schema_match`, `exact_match`, `semantic_similarity` |
| [`multi_turn_support/`](multi_turn_support/) | Multi-turn customer-support conversation, driven by a simulated user | `user_simulator` adapter, transcript-scope `llm_judge` |
| [`thinking_eval/`](thinking_eval/) | Claude extended thinking — was it present, proportionate, and did it leak? | `thinking_present`, `thinking_tokens_under`, `thinking_does_not_leak` |
| [`coding_agent/`](coding_agent/) | Agent patches a fixture repo → grader runs pytest in the artifact dir | `tempdir_snapshot` workspace, `command` evaluator |

### Comparison and fleets
| Example | What it evaluates | Anchor features |
|---|---|---|
| [`model_fleet/`](model_fleet/) | Same task across three models (Claude Haiku, Claude Sonnet, GPT) — quality, cost, latency | Three variants, `run.cost_limit_usd`, ComparisonReport |
| [`listing_price/`](listing_price/) | HTTP agent service template — two variants, LLM judge | `http` adapter with request templating, response JSONPath extraction |

### CI and operations
| Example | What it evaluates | Anchor features |
|---|---|---|
| [`regression_gate/`](regression_gate/) | Drift detection workflow — `evalh run` → `promote` → `drift` → CI gate | Deterministic agent, committed baseline run, no API keys |
| [`slack_drift_notify/`](slack_drift_notify/) | Same drift workflow as above, with Slack notifications on regression | `webhook` trace store, `output:` as a list of sinks |
| [`distributed_ray/`](distributed_ray/) | Same workflow on a Ray cluster — config travels, code doesn't | `run.executor.type: ray`, capacity pools |

### Online evaluation
| Example | What it evaluates | Anchor features |
|---|---|---|
| [`online_eval/`](online_eval/) | Score production traffic offline by replaying historical traces | `fixture` dataset with `embed_full_trace`, `replay` system adapter |
| [`observability_langfuse/`](observability_langfuse/) | Langfuse as dataset source, trace sink, and trace enricher — three patterns in one config | `langfuse` triplet (DatasetAdapter / TraceStore / TraceEnricher) |

Every example has its own README with file table, required env, the run command, "what happens in order", and "extending it" notes.

---

## Adding a new example

Eight rules every example follows. Match them and your example will land cleanly.

1. **Public-safe.** This repo is public. Sample emails are `you@example.com`; no real customer data, no real API endpoints, no secrets in fixtures.
2. **Current models only.** Use `claude-4-7`, `claude-haiku-4-5`, `gpt-5.5`. Never name deprecated models.
3. **YAML for human-authored files.** `eval.yaml`, `cases.yaml`. JSONL is for the machine artefacts in `runs/`.
4. **Async everywhere.** Any `agent.py` is `async def` and uses `httpx.AsyncClient`, not `requests`.
5. **One example, one feature.** The reader should be able to summarise what it demonstrates in one sentence. No drive-by additions.
6. **README in each example.** Match the [coding_agent/README.md](coding_agent/README.md) shape: file table, required env, run command, "what happens in order", "why this works", "extending it".
7. **Offline-runnable wherever possible.** If it *can* run without an API key (fixtures + `replay`, or `python_function` against a deterministic stub), it should. Real-LLM examples are explicitly labelled and excluded from CI smoke runs.
8. **Optional extras fail at plan time, not at import time.** If the example needs an extra (`anthropic`, `openai`, `embeddings_local`, `ray`, etc.), guard the import inside the adapter / agent and raise `eval_harness.core.errors.ConfigError` with the install command. Mirror the `sqlite_store.py` pattern.

Two additional conventions for evaluators and tests:

- **Every evaluator is either gating or labelled `# informational — not gating`.** No silent decoration. If an evaluator's pass/fail doesn't affect the summary, say so in a one-line YAML comment above it.
- **Tests for an example live in their own file.** `tests/unit/test_examples/test_<example_name>.py`. Don't add to shared adapter test files unless you have a strong reason — namespace isolation by file prevents parallel-contributor collisions.

---

## Running them

```bash
# Offline examples — work on a fresh checkout
evalh run examples/tiny_demo/eval.yaml          # needs ANTHROPIC_API_KEY
evalh run examples/online_eval/eval.yaml         # no key needed
evalh run examples/regression_gate/eval.yaml     # no key needed
evalh run examples/streaming_chat/eval.yaml      # no key needed
evalh run examples/text_to_sql/eval.yaml         # no key needed
evalh run examples/slack_drift_notify/eval.yaml  # webhook short-circuits if SLACK_WEBHOOK_URL unset

# Needs `[embeddings_local]` extra (sentence-transformers, ~80 MB)
evalh run examples/rag_qa/eval.yaml
evalh run examples/structured_extraction/eval.yaml
evalh run examples/observability_langfuse/eval.yaml

# Needs ANTHROPIC_API_KEY (and possibly OPENAI_API_KEY)
evalh run examples/coding_agent/eval.yaml
evalh run examples/model_fleet/eval.yaml          # needs BOTH keys
evalh run examples/multi_turn_support/eval.yaml
evalh run examples/thinking_eval/eval.yaml
evalh run examples/listing_price/eval.yaml        # also needs your HTTP agent service

# Needs ray[default] + a cluster (or local Ray)
evalh run examples/distributed_ray/eval.yaml
```
