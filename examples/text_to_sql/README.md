# Example: text_to_sql

A text-to-SQL agent eval with **execution grading**. The agent turns each natural-language question into a JSON envelope (`{"sql": ..., "intent": ...}`); the harness then validates the envelope's shape with `schema_match`, and the `command` evaluator runs the agent's SQL against a fixture SQLite database to check that it returns the right rows.

This is the canonical shape for "the agent under test produces code (SQL, in this case); grade by executing the code and diffing outputs."

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | Wires `python_function` agent + `tempdir_snapshot` workspace + `schema_match` + `command` evaluators. |
| [`cases.yaml`](cases.yaml) | Three questions over the fixture DB; each names its expected-CSV. |
| [`agent.py`](agent.py) | The agent — emits a JSON envelope, stages `query.sql` / `expected.csv` / `compare.py` into the workspace. |
| [`compare.py`](compare.py) | Execution grader: runs `query.sql` against `db.sqlite`, diffs rows against `expected.csv`. |
| [`fixtures/db.sqlite`](fixtures/db.sqlite) | Seeded SQLite DB: three customers, six orders. Synthetic, public-safe. |
| [`fixtures/expected_*.csv`](fixtures/) | Canonical row-set per case. |
| [`fixtures/seed.py`](fixtures/seed.py) | Helper that regenerates `db.sqlite` byte-equivalently. Run it to audit the binary. |

## Required environment

Default mode is **offline** — no API key, no extras. The shipped agent has a deterministic stub mapping each case to canonical SQL, so the run exercises the full evaluator stack without external calls.

To swap in Claude as the SQL generator:

```bash
export ANTHROPIC_API_KEY=...
export EVALH_TEXT_TO_SQL_USE_LLM=1
pip install 'eval-harness[anthropic]'
```

If `EVALH_TEXT_TO_SQL_USE_LLM=1` is set without `anthropic` installed, the agent raises `ConfigError` with the install command — never a bare `ImportError`.

## Run it

```bash
evalh run examples/text_to_sql/eval.yaml
```

Expected runtime: under 5s for the three shipped cases in offline mode.

## What happens, in order

1. `tempdir_snapshot` copies the contents of `fixtures/` (the DB, all expected CSVs, and `seed.py`) into a fresh per-case temp directory.
2. The `python_function` adapter invokes `agent.run(case, variant)`. The adapter exposes the working copy at `variant["_workspace_path"]`.
3. The agent decides on SQL (stub by default; Claude when `EVALH_TEXT_TO_SQL_USE_LLM=1`) and returns `{"final_answer": ..., "structured": {"sql": ..., "intent": ...}, "metrics": {...}}`. Setting `structured` flows straight to `Trace.output.structured`.
4. Before returning, the agent writes three files into the workspace: `query.sql` (its SQL), `expected.csv` (the case's chosen expected file, copied), and `compare.py` (copied from this example's root).
5. The workspace adapter snapshots the post-edit tree as a `FilesystemArtifact`. That snapshot dir is what the `command` evaluator's `cwd` resolves to.
6. **`schema_match`** validates that `Trace.output.structured` matches the JSON envelope — `{sql: string, intent: string}`, no extra keys. Failures here mean the agent didn't produce the contract shape.
7. **`command`** runs `python compare.py` in the artifact dir. `compare.py` reads `query.sql`, executes it against `db.sqlite`, and diffs the rowset (sorted, with floats canonicalised via `:g`) against `expected.csv`. Pass iff exit code 0.
8. `final_answer_nonempty` is `contains_text` looking for `SELECT` — informational only, kept off `pass_criteria` so a chatty `final_answer` never overrides the execution-grading verdict.

## Why this works

It is the first example of `schema_match` in the repo — the anchor for that evaluator. The envelope-as-contract pattern (agent emits typed JSON, harness validates shape, then a downstream tool consumes the typed fields) generalises to any agent that produces structured output.

It's also the first example of *execution grading* — running the agent's output and comparing observable behaviour. For code, SQL, configs, or any other artifact where a unit test or executor exists, this beats LLM-judged correctness on every axis: cheap, deterministic, falsifiable.

## Extending it

- **Add a case**: drop a new entry into `cases.yaml`, ship its `expected_<id>.csv` under `fixtures/`, and (if running in stub mode) add a `_STUB[case.id]` mapping in [`agent.py`](agent.py). In LLM mode the model handles new questions on its own — the new case will pass iff Claude's SQL returns the right rows.
- **Stricter envelope**: tighten the schema in [`eval.yaml`](eval.yaml) — e.g. require `intent` to match an enum, or add `confidence: {type: number, minimum: 0, maximum: 1}` and have the agent emit it.
- **Multiple agents, same DB**: add a second entry to `systems:` with a different `target:`. The runner will dispatch `cases × 2` and the per-variant summary will show which agent produced more matching rowsets.
- **Schema-aware prompting**: have the agent first ask SQLite for the schema (`PRAGMA table_info(...)`) and include it in the prompt. The fixture stays the same; the agent gets more general.
- **Rebuild the DB**: `python examples/text_to_sql/fixtures/seed.py` regenerates `db.sqlite` from `seed.py`'s tables. Useful if you change the schema or rows.
