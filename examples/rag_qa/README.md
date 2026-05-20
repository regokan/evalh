# Example: rag_qa

A retrieval-augmented QA pipeline over a tiny synthetic corpus of company-policy docs. The agent runs a deterministic in-memory retriever, then a generator that composes an answer from the retrieved documents. Four evaluators grade the run: recall on the retrieved doc IDs, entity coverage in the answer, faithfulness/grounding via an LLM judge, and semantic closeness to a curated reference.

This is the canonical shape for "I have a RAG system over my docs — how do I evaluate retrieval recall AND answer quality at the same time?" It is also the first example of the `semantic_similarity` evaluator.

## Files

| File | What it is |
|---|---|
| [`agent.py`](agent.py) | The agent — keyword retriever + stub/LLM generator. Async. ~90 lines. |
| [`cases.yaml`](cases.yaml) | Four questions over the corpus. Each carries the expected retrieved doc IDs and a curated reference answer. |
| [`eval.yaml`](eval.yaml) | Wires `python_function` agent + all four evaluators (`tool_called`, `contains_text`, `llm_judge`, `semantic_similarity`). |
| [`fixtures/docs.jsonl`](fixtures/docs.jsonl) | 12 synthetic policy docs (PTO, remote work, expenses, IT, handbook). |
| [`fixtures/reference_answers.yaml`](fixtures/reference_answers.yaml) | Human-readable canonical answers, mirrored into `cases.yaml > expected.facts.reference_answer` for the semantic_similarity evaluator. |

## Required environment

The default run is **offline** — no API key needed. You do need two install-time extras:

```bash
pip install 'eval-harness[anthropic,embeddings_local]'
# or with uv:  uv sync --extra anthropic --extra embeddings_local
```

- `[anthropic]` lets the `llm_judge` backend instantiate at plan time. Without `ANTHROPIC_API_KEY` exported, the judge call itself errors and that one evaluator reports an error — the deterministic gates in `pass_criteria` still decide run outcome, so the run still finishes cleanly.
- `[embeddings_local]` pulls `sentence-transformers` (~80 MB on first run) for the local embedder backing `semantic_similarity`.

To swap the deterministic stub generator for Claude:

```bash
export ANTHROPIC_API_KEY=...
export EVALH_RAG_USE_LLM=1
```

## Run it

```bash
evalh run examples/rag_qa/eval.yaml
```

Expected runtime: under 5s for the four shipped cases on a warm cache (model download dominates the first run).

## What happens, in order

1. The runner expands `cases × variants` — four cases, one variant.
2. For each case the `python_function` adapter calls `agent.run(case, variant)`.
3. The agent tokenises the question (lowercase, stopwords removed), scores each of the 12 docs by token overlap, picks the top 2, and records them as a `retriever` tool call with `arguments.doc_ids` set to the sorted retrieved IDs.
4. The default stub generator stitches the first sentence of each retrieved doc into the final answer. With `EVALH_RAG_USE_LLM=1` plus `ANTHROPIC_API_KEY`, Claude-Haiku generates the answer from the same retrieved context instead.
5. Evaluators run against the trace:
   - `tool_called` checks `arguments.doc_ids` against the per-case `expected.facts.retrieved_doc_ids` (recall@k as a hard equality gate).
   - `contains_text` defaults its `all_of` to `case.expected.answer_should_include` and checks the answer text.
   - `llm_judge` asks Claude-Haiku whether the answer only uses facts from the retrieved docs.
   - `semantic_similarity` cosine-compares the answer against the case's reference via `sentence-transformers/all-MiniLM-L6-v2`, threshold 0.75.
6. Results land in `runs/<run_id>/{config.yaml,traces.jsonl,results.jsonl,summary.yaml}`.

## Why this works

A real RAG eval has to grade two things that pull in different directions: **retrieval** ("did we find the right documents?") and **generation** ("is the answer correct, grounded, and well-phrased?"). Either alone is misleading — perfect retrieval can still produce a hallucinated answer, and a fluent answer can be built on top of the wrong documents.

The four evaluators cover both axes:

- `tool_called` (retrieval): hard gate on the retrieved doc set.
- `contains_text` (generation, deterministic): the answer mentions a required entity.
- `llm_judge` (generation, semantic): the answer is grounded in the retrieved docs.
- `semantic_similarity` (generation, embedding-based): the answer is close to a curated reference.

Recording the retriever as a tool call (rather than as side-channel state) is what lets `tool_called` grade retrieval the same way it grades any other tool — the same evaluator that grades "did the agent call `get_listing_details`?" also grades "did the retriever return the right docs?". The trace is the system of record.

## Extending it

- **Add cases**: append to `cases.yaml` and add the matching reference in `fixtures/reference_answers.yaml`. Keep `expected.facts.retrieved_doc_ids` alphabetically sorted — the `tool_called` evaluator does exact list equality.
- **Swap the corpus**: replace `fixtures/docs.jsonl`. Re-tune `_TOP_K` in [`agent.py`](agent.py) if your corpus needs more recall headroom.
- **Swap retrievers**: replace `_retrieve` with a call to your own retriever (FAISS, BM25, your vector DB). The contract is just "given a question, return a list of `{id, title, text}` dicts." The evaluator shape doesn't change.
- **Swap embedders**: change `embedder_name` in `eval.yaml` to `openai-text-embedding-3-small` (and `pip install 'eval-harness[openai]'`) to use the OpenAI API-backed embedder instead of the local model.
- **Real LLM generator**: set `EVALH_RAG_USE_LLM=1` and `ANTHROPIC_API_KEY` for a Claude-backed generator; the stub stays in place as the offline path.
