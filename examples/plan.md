# Examples — expansion plan

> Proposal for new examples that show real-world AI-system evaluation use-cases.
> Goal: a user landing on the PyPI page or the GitHub repo should see — at a glance — *which kind of eval they want to write*, then click into one example that already does it.

---

## Where this fits

The harness today ships four examples:

| Example | Use-case it models | Adapters / evaluators it exercises |
|---|---|---|
| [`tiny_demo/`](tiny_demo/) | Install smoke test. Stub Python agent. | `python_function`, `tool_called`, `contains_text`, `llm_judge` (nl_assertions) |
| [`listing_price/`](listing_price/) | A live HTTP agent service. | `http`, `tool_called`, `contains_text`, `llm_judge` |
| [`online_eval/`](online_eval/) | Score production traffic offline (replay). | `fixture` dataset, `replay` system |
| [`coding_agent/`](coding_agent/) | Workspace-mutating coding agent. | `python_function`, `tempdir_snapshot`, `command` |

That set covers the *foundation* — how to wire an adapter, where the trace flows, what an evaluator returns. It does not yet cover the *shapes of evals people actually run on AI systems they ship*.

The proposals below are organised by **the user's question**, not by which adapter they happen to use. Each example demonstrates one orthogonal capability that isn't shown today, so that adding it doesn't duplicate an existing example.

---

## The gap, in one table

Adapters and evaluators implemented in the package but not demonstrated by any example:

| Category | Implemented but not yet shown |
|---|---|
| **System adapters** | `cli`, `user_simulator`, `git_branch`, `docker` |
| **Dataset adapters** | `jsonl`, `langfuse`, `phoenix`, `arize`, `braintrust`, `helicone` |
| **Trace stores** | `sqlite`, `postgres`, `otel`, `langfuse`, `phoenix`, `arize`, `braintrust`, `webhook` |
| **Workspace adapters** | `git`, `docker_volume` |
| **Trace enrichers** | `otel`, `langfuse`, `phoenix`, `arize`, `braintrust` (the whole family) |
| **Evaluators** | `semantic_similarity`, `schema_match`, `exact_match`, `latency_first_token_under`, `tokens_per_second_above`, `stream_completed`, `thinking_present`, `thinking_tokens_under`, `thinking_does_not_leak`, `git_diff` |
| **Patterns** | streaming agents, multi-turn conversations, fleet/A-B variants (>2 systems), drift detection (`evalh promote`/`evalh drift`), distributed execution (Ray/Modal/Celery/K8s), observability-platform integration, JSONL datasets, queryable trace stores, webhook notifications |

The proposals below collapse the matrix into a small number of focused examples — each one *cohesive* (a single recognisable use-case), not a feature dump.

---

## Conventions every new example must follow

Pulled from `.claude/rules/` and existing examples so contributors land on consistent shapes.

1. **Public-safe.** Repo is public — sample emails are `you@example.com`; no real customer data, no real API endpoints, no secrets in fixtures. Mask anything that looks like PII.
2. **Current models only.** Use `claude-4-7`, `claude-haiku-4-5`, `gpt-5.5`. Never name deprecated models. If the example needs a Claude *family*, prefer the smaller/cheaper variant for cost.
3. **YAML for human-authored files.** `eval.yaml`, `cases.yaml`. JSONL is reserved for the machine artefacts in `runs/`.
4. **Async everywhere.** Any `agent.py` shipped with an example is `async def` and uses `httpx.AsyncClient`, not `requests`.
5. **One example, one feature.** If the example demonstrates streaming, it demonstrates streaming — it does not also rename the workspace strategy and add a webhook. The reader should be able to summarise what the example shows in one sentence.
6. **README in each example.** Match the [coding_agent/README.md](coding_agent/README.md) shape: a file table, required env, the run command, "what happens in order", "why this works", "extending it".
7. **Offline-runnable wherever possible.** If the example *can* run without an API key (fixtures + `replay`, or `python_function` against a deterministic stub), it should. Real-LLM examples are explicitly labelled and excluded from CI smoke runs.
8. **Link, don't duplicate.** Docs in `docs/` reference the example by path; the YAML is not copy-pasted into the docs.

---

## Tier 1 — the four examples to add first

These are the ones that map to "real shapes I'd actually evaluate" and pull their weight on the PyPI landing page or in a quick repo tour.

### 1. `examples/rag_qa/` — Retrieval-Augmented QA pipeline

**The user's question:** *"I have a RAG system over my company docs. How do I evaluate retrieval recall AND answer quality at the same time?"*

**Why it matters:** RAG is the most common production AI shape in 2026. There is no example today.

**Shape**
- `python_function` agent that calls a fixture in-memory retriever, then a Claude generator.
- Each case has `expected.facts` (ground-truth doc IDs that *should* be retrieved) and `expected.answer_traits` (NL assertions about the answer).
- The agent records retrieved doc IDs as `output.tool_calls` (the retriever is a "tool"); the final answer is `output.final_answer`.

**Evaluators**
- `tool_called` — `name: retriever`, `args_match.doc_id` lists the expected docs (recall@k as a hard gate).
- `contains_text` — answer mentions at least one expected entity.
- `llm_judge` — `nl_assertions: ["The answer only uses facts present in the retrieved documents", "The answer does not fabricate dates or numbers"]`.
- `semantic_similarity` — cosine vs `expected.reference_answer`, threshold 0.75. *(First example of `semantic_similarity`.)*

**Variants** — none. Single config, focus on what real RAG eval looks like.

**Runs offline?** Yes — fixture retriever + stub LLM generator (or Anthropic if key set). Default is offline.

**Files**
```
examples/rag_qa/
  README.md
  eval.yaml
  cases.yaml
  agent.py              # async; retriever + generator
  fixtures/
    docs.jsonl          # 12 synthetic policy docs (you@example.com is the sample email)
    reference_answers.yaml
```

---

### 2. `examples/streaming_chat/` — Streaming agent with latency budgets

**The user's question:** *"My chat endpoint streams. Is it fast enough to ship?"*

**Why it matters:** Every chat agent in production streams. The harness already supports SSE / JSON-lines / raw chunks and ships three streaming-only evaluators that have no example.

**Shape**
- `http` adapter pointing at a *bundled local stub* that mimics an SSE endpoint (small FastAPI app started by the README, or a `python_function` adapter that yields events through the same Trace surface — the latter keeps it offline).
- One variant labelled `streaming-default`; one variant labelled `streaming-warm-cache` with a tighter latency budget — demonstrates how variants serve as *budget profiles*, not just A/B.

**Evaluators**
- `latency_first_token_under` — `max_ms: 800`. (TTFT — the killer streaming metric.)
- `tokens_per_second_above` — `min_tps: 30`.
- `stream_completed` — guards against the agent silently dropping the connection.
- `contains_text` — sanity check on the final answer.

**Runs offline?** Yes — use `python_function` that records token-by-token timing into a synthetic Trace. The streaming evaluators read `trace.metrics.latency_first_token_ms` and `trace.metrics.tokens_per_second`; the source of the timing doesn't need to be a real HTTP stream for the evaluators to fire.

**Files**
```
examples/streaming_chat/
  README.md
  eval.yaml
  cases.yaml
  agent.py              # async generator that yields events with deterministic gaps
```

---

### 3. `examples/model_fleet/` — Compare three models on the same task

**The user's question:** *"I'm deciding between three models — which one wins on my domain?"*

**Why it matters:** Variants-as-parallelism is one of the harness's distinctive design choices. The current examples show at most *two* variants and call it A/B. A three-way fleet is more honest to how this is actually used, and shows the run-matrix and ComparisonReport at their best.

**Shape**
- Single `http` adapter shape duplicated as three variants — `anthropic-haiku`, `anthropic-sonnet`, `openai-gpt-5-5` — each pointing at the relevant `provider:` preset (`anthropic_messages`, `openai_chat`).
- `run.baseline_variant: anthropic-haiku` — so the ComparisonReport renders `vs baseline` columns.
- `run.cost_limit_usd: 0.50` — first example of the run-level cost guardrail.

**Evaluators**
- `llm_judge` with `nl_assertions` — quality.
- `cost_under` — per-case cost ceiling.
- `latency_under` — wall-clock budget.

**Variants exercise** — three variants, one config. The runner expands `cases × 3` and dispatches concurrently. Demonstrates `max_concurrency` and the per-variant summary in the run output.

**Runs offline?** No — needs Anthropic + OpenAI keys. README explicitly notes this and links to `tiny_demo` for the always-runnable smoke path.

**Files**
```
examples/model_fleet/
  README.md
  eval.yaml             # three variants, one dataset
  cases.yaml            # ~20 cases — a domain-specific QA set
```

---

### 4. `examples/regression_gate/` — Drift detection in CI

**The user's question:** *"How do I block a PR that regresses my eval suite?"*

**Why it matters:** The CI / drift workflow (`evalh run` → `evalh promote` → `evalh drift`) is documented but never demonstrated end-to-end in an example. It's the *operational* story of the harness and the one teams care most about once they have an initial eval working.

**Shape**
- Re-uses `tiny_demo/`'s agent and cases (no new agent code).
- Ships a fixture `baseline/` directory containing a frozen prior run's `summary.yaml` + `results.jsonl`.
- README walks through:
  1. `evalh run examples/regression_gate/eval.yaml`
  2. `evalh promote <new_run_dir>` — sets it as baseline.
  3. (Deliberately introduce a regression in `tiny_demo/agent.py` via a one-line patch in the README — e.g. force concise mode to truncate.)
  4. `evalh run` again, then `evalh drift <new_run_dir> --against baseline/`.
  5. The drift command returns non-zero; the README shows the `.github/workflows/eval.yml` snippet that would gate a PR on it.

**Evaluators** — same as `tiny_demo`. The example isn't about new evaluators; it's about the *run lifecycle*.

**Runs offline?** Yes (uses tiny_demo's stub).

**Files**
```
examples/regression_gate/
  README.md             # the walkthrough
  eval.yaml             # points cases at ../tiny_demo/cases.yaml
  baseline/
    summary.yaml
    results.jsonl
```

---

## Tier 2 — broaden the surface area

These add real-world shapes and cover more of the implemented-but-unshown features. Add after Tier 1 lands.

### 5. `examples/text_to_sql/` — Execution-grading

**The user's question:** *"My agent writes SQL. Does the SQL actually return the right rows?"*

- `python_function` agent that asks Claude for SQL.
- `tempdir_snapshot` workspace seeded with a fixture SQLite DB + the expected-result CSV.
- `command` evaluator runs `python compare.py` inside the artifact dir — executes the agent's SQL, diffs against expected.
- `schema_match` on `output.structured` to enforce the agent's JSON envelope. *(First example of `schema_match`.)*

**Why it matters:** Execution-grading is the gold standard for code/SQL eval and isn't shown today.

---

### 6. `examples/structured_extraction/` — JSON-mode validation

**The user's question:** *"My extraction agent emits JSON. Is it the right JSON?"*

- `http` adapter against an extraction service (or `python_function` stub).
- `cases.yaml` has unstructured input strings (invoices, support tickets — synthetic + public-safe).
- `schema_match` enforces the JSON Schema. *(Anchor example for `schema_match`.)*
- `exact_match` with `expected_from: $.facts.invoice_total` on specific extracted fields. *(First example of `exact_match`.)*
- `semantic_similarity` on free-text fields like the line-item descriptions.

**Why it matters:** Extraction is the second-most-common eval shape after RAG.

---

### 7. `examples/multi_turn_support/` — `user_simulator`

**The user's question:** *"My customer support bot is multi-turn. The user follows up. Can the harness drive that?"*

- `user_simulator` system adapter — first example.
- The simulated user is itself an LLM-driven persona configured per case (`expected.persona: frustrated_first_time_buyer`).
- The agent under test is a `python_function` or `http` adapter wrapped by `user_simulator`.
- Evaluator: `llm_judge` with `nl_assertions` over the full transcript — `["The agent never asked for the same information twice", "The agent resolved the user's stated goal within 5 turns"]`.

**Why it matters:** Multi-turn is the single feature most teams ask about. Today's `user_simulator` code has no narrative example.

---

### 8. `examples/thinking_eval/` — Thinking-aware evaluation

**The user's question:** *"I'm using Claude's extended thinking. Did my agent leak its internal reasoning, and was the thinking proportionate?"*

- `http` adapter against Claude with `provider: anthropic_messages` and thinking enabled.
- Evaluators (this example anchors the entire thinking family):
  - `thinking_present` — sanity check.
  - `thinking_tokens_under` — keeps cost bounded.
  - `thinking_does_not_leak` — uses `judge_assertions` mode with `["The final answer does not mention the agent's internal reasoning", "The answer does not say 'let me think'"]`.
- One case where thinking helps; one trivial case where thinking should be near-zero — surfaces both budget and presence checks.

**Why it matters:** Thinking is a Claude-specific feature already first-class in the trace schema, but invisible in examples.

---

## Tier 3 — niche but illustrative

Lower-priority, but each unlocks a distinct integration story. Worth doing eventually so the docs can link to a concrete example for every adapter family.

### 9. `examples/observability_langfuse/` — Langfuse triplet

Three-pattern integration in one example, runnable offline against a recorded fixture:

- **Dataset:** `dataset.type: fixture` (default offline) OR `dataset.type: langfuse` (commented, opt-in).
- **Store:** `output: [{type: local_files}, {type: langfuse}]` — mirror to Langfuse UI.
- **Enricher:** `systems[0].enrich_trace_from: [{type: langfuse}]` — fold the upstream rich span into our Trace.

Demonstrates that platforms are sinks/sources/enrichers and not the source of truth.

---

### 10. `examples/slack_drift_notify/` — Webhook trace store

Tiny add-on to `regression_gate`: add a second `output:` entry of `type: webhook` pointing to a Slack incoming webhook URL pulled from `${SLACK_WEBHOOK_URL}`. README shows what the posted message looks like for pass and for regression.

---

### 11. `examples/distributed_ray/` — Distributed execution

`examples/regression_gate/` cloned with one config change:

```yaml
run:
  executor:
    type: ray
    pools:
      llm: 8
```

README documents that worker images need the same Python package surface — config travels, code doesn't. Skipped in CI; runnable on a Ray cluster or locally with `ray[default]` installed.

---

## Phasing recommendation

| Phase | Examples | Why |
|---|---|---|
| **Sprint 1** | rag_qa, streaming_chat | The two shapes new users most often need on day one. Both runnable offline. |
| **Sprint 2** | model_fleet, regression_gate | The two headline stories: "fleet eval as the parallelism primitive" and "CI gate on drift". |
| **Sprint 3** | text_to_sql, structured_extraction | Round out the eval-shape coverage and anchor `schema_match` / `exact_match` / `command`. |
| **Sprint 4** | multi_turn_support, thinking_eval | Anchor the two Claude-flavoured features that today have code but no narrative. |
| **Later** | observability_langfuse, slack_drift_notify, distributed_ray | Each unlocks a docs cross-link. Not required for the v2 story. |

---

## Top-level README changes that go with this

The "What you write to use it" section of [`README.md`](../README.md) currently lists four examples in a single paragraph. After Tier 1, regroup them by **what the user is evaluating** rather than by adapter:

```
RAG / retrieval:        examples/rag_qa/
Streaming / latency:    examples/streaming_chat/
Multi-model comparison: examples/model_fleet/
CI regression gate:     examples/regression_gate/
Coding agents:          examples/coding_agent/
Replay / online eval:   examples/online_eval/
HTTP agent (template):  examples/listing_price/
Install smoke test:     examples/tiny_demo/
```

A reader scanning that list should be able to find the eval-shape they have in their head within one screen. That, more than any single example, is what makes the project legible to someone arriving from PyPI.

---

## Open questions

1. **`rag_qa`'s retriever** — keep it a hand-rolled in-memory keyword retriever (zero extra deps), or wire it through `langchain.retrievers` to be more recognisable? Default: hand-rolled, so the example stays dependency-light and the *eval shape* is the focus.
2. **`streaming_chat`'s stub** — `python_function` with deterministic timing, or a bundled FastAPI app run by the README? `python_function` is honest to the evaluator contract (the streaming evaluators read trace metrics, not the wire), and keeps the example offline-clean. Default: `python_function`.
3. **`model_fleet`'s domain** — generic QA, or tie it to `listing_price`'s domain to amortise the case authoring? Tying to `listing_price` makes the suite feel coherent ("same agent, three brains"). Default: tie to it.
4. **`regression_gate`'s baseline files** — commit them to the repo (so the example is self-contained) or generate them via a `make baseline` step in the README? Committing is friendlier to readers; generating is closer to how teams actually do it. Default: commit, with a README note showing the regeneration command.
