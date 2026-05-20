# Example: structured_extraction

An invoice-extraction agent eval. The agent receives an unstructured invoice string, parses it into a canonical JSON envelope (`invoice_id`, `vendor`, `invoice_total`, `currency`, `line_items`), and three evaluators grade the run: the envelope conforms to a JSON Schema, the extracted total exactly matches the per-case ground truth, and a one-sentence summary is semantically close to a curated reference.

This is the canonical shape for "my extraction agent emits JSON — is it the right JSON?". It is the **anchor example for `schema_match`** and the **first example of `exact_match`** in the repo.

> **Deviation from `examples/plan.md`.** The plan describes the `schema_match` config as a path to `schemas/invoice.schema.json`. The shipped `schema_match` evaluator takes a `dict`, not a path, so the schema is **duplicated** here: the `.json` file is the human-readable canonical artefact, and `eval.yaml > evaluators.envelope_matches_schema.config.schema` mirrors it. Adding `schema_path` support to the evaluator is deferred to a follow-up; this example fails the "Link, don't duplicate" convention in exactly one place and calls it out so future readers don't have to discover it.

## Files

| File | What it is |
|---|---|
| [`agent.py`](agent.py) | The agent — regex-based extractor by default, optional Claude path. Async. ~120 lines. |
| [`cases.yaml`](cases.yaml) | Four synthetic invoices. Each carries `invoice_total` (for `exact_match`) and a `reference_summary` (for `semantic_similarity`). |
| [`eval.yaml`](eval.yaml) | Wires `python_function` agent + the three evaluators (`schema_match`, `exact_match`, `semantic_similarity`). |
| [`schemas/invoice.schema.json`](schemas/invoice.schema.json) | JSON Schema for the invoice envelope. Loaded by `agent.py` for the LLM path; mirrored into `eval.yaml` for `schema_match`. |

## Required environment

The default run is **offline** — no API key needed. You do need one install-time extra:

```bash
pip install 'eval-harness[embeddings_local]'
# or with uv:  uv sync --extra embeddings_local
```

`[embeddings_local]` pulls `sentence-transformers` (~80 MB on first run) for the local embedder backing `semantic_similarity`.

To swap the deterministic regex extractor for Claude:

```bash
pip install 'eval-harness[anthropic,embeddings_local]'
export ANTHROPIC_API_KEY=...
export EVALH_EXTRACTION_USE_LLM=1
```

If you set `EVALH_EXTRACTION_USE_LLM=1` without the `[anthropic]` extra installed, the agent raises `ConfigError` at the first case with an install hint — no `ImportError` surfaces at plan time.

## Run it

```bash
evalh run examples/structured_extraction/eval.yaml
```

Expected runtime: under 5s for the four shipped cases on a warm cache (model download dominates the first run).

## What happens, in order

1. The runner expands `cases × variants` — four cases, one variant.
2. For each case the `python_function` adapter calls `agent.run(case, variant)`.
3. The agent reads the invoice text and either (a) runs the deterministic regex extractor, or (b) — if `EVALH_EXTRACTION_USE_LLM=1` plus `ANTHROPIC_API_KEY` — asks Claude-Haiku for the same JSON envelope.
4. The agent returns `{"structured": <envelope>, "final_answer": <one-sentence summary>}`. The `python_function` adapter places the envelope on `trace.output.structured` and the summary on `trace.output.final_answer`.
5. Evaluators run against the trace:
   - `schema_match` validates `output.structured` against the inline schema.
   - `exact_match` compares `output.structured.invoice_total` against `case.expected.facts.invoice_total`.
   - `semantic_similarity` cosine-compares `output.final_answer` against the case's `reference_summary` via `sentence-transformers/all-MiniLM-L6-v2`, threshold 0.70.
6. Results land in `runs/<run_id>/{config.yaml,traces.jsonl,results.jsonl,summary.yaml}`.

## Why this works

Extraction is the second-most-common production AI shape after RAG, and it pulls in two directions that a single evaluator can't cover:

- **Structural correctness.** The envelope must have the right keys, types, and enum values — otherwise downstream code that consumes the JSON breaks regardless of how "right" the content looks. `schema_match` is the deterministic gate that catches this.
- **Value correctness.** A schema-valid envelope can still get the wrong total. `exact_match` on `invoice_total` is the per-field ground-truth check that catches it.
- **Free-text fidelity.** Free-text fields (line-item descriptions, a summary) won't match exactly — they need a fuzzy similarity score, and `semantic_similarity` provides it.

All three evaluators are wired into `pass_criteria.all_required`: every evaluator in this example is gating. That follows the Tier 1 retrospective lesson — silent decorative evaluators are a footgun, so anything not gating must be explicitly labelled informational (no informational evaluators here).

## Extending it

- **Add cases**: append to `cases.yaml`. Each case needs `input.invoice_text`, `expected.facts.invoice_total`, and `expected.facts.reference_summary`.
- **Swap the schema**: edit `schemas/invoice.schema.json` and keep the inline copy in [`eval.yaml`](eval.yaml) in sync. The `additionalProperties: false` constraint makes typos in extracted field names hard failures.
- **Tighten the evaluators**: drop `additionalProperties: false` to allow extra keys, or raise `semantic_similarity.threshold` above 0.70 if your extractor produces tighter summaries.
- **Real LLM extractor**: set `EVALH_EXTRACTION_USE_LLM=1` and `ANTHROPIC_API_KEY` for a Claude-backed extractor; the regex stub stays in place as the offline path.
- **Switch domains**: replace the schema and cases with your own — receipt parsing, support-ticket categorisation, resume parsing. The eval shape (schema + exact-value + summary) is the reusable part.
