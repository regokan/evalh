# CLI reference (`evalh`)

The package ships one binary: `evalh`. Every command takes a path to a config or a run directory.

## Commands

### `evalh run <eval.yaml>`

Execute an eval. Lands in v0.

```bash
evalh run examples/tiny_demo/eval.yaml
```

What happens:
1. Validate the config (Pydantic) and resolve `${VAR}` from `os.environ`.
2. Load cases via the `DatasetAdapter`.
3. Build the run matrix (`cases × variants`).
4. Dispatch concurrently, bounded by `run.max_concurrency`.
5. Write `runs/<run_id>/` with `config.yaml`, `traces.jsonl`, `results.jsonl`, `summary.yaml`.

**Exit codes**: `0` if every case completed without an *infrastructure* error (a hung adapter, a workspace crash). Per-case evaluator failures are not run-level failures — they're reported in `summary.yaml`.

**Useful flags** (v0):
- `--dry-run` — validate config, expand the matrix, print what would run, exit.
- `--limit N` — process only the first N cases (cheap sanity check).
- `--filter 'metadata.suburb=Richmond'` — subset by case metadata.
- `--max-concurrency N` — override `run.max_concurrency` from CLI.

### `evalh inspect <run_dir>`

Pretty-print one run. Lands in v0.1.

```bash
evalh inspect runs/2026-05-08T14-22-00_listing_price_eval --case listing_price_001
evalh inspect runs/2026-05-08T14-22-00_listing_price_eval --variant agent_main
evalh inspect runs/2026-05-08T14-22-00_listing_price_eval --failed
```

Pulls from `traces.jsonl` + `results.jsonl` for the requested case/variant, renders as a human-readable view (messages, tool calls, evaluator results).

### `evalh compare <run_a> <run_b>`

Diff two runs. Lands in v0.1.

```bash
evalh compare runs/<run_id_before> runs/<run_id_after>
```

Shows per-case status flips: cases that passed in `<run_a>` and failed in `<run_b>` (regressions) and vice-versa (improvements). Use to A/B two full runs against the same dataset.

### `evalh re-evaluate <run_dir>`

Re-run only the evaluators against existing traces. Lands in v0.1.

```bash
evalh re-evaluate runs/<run_id>                   # rerun all configured evaluators
evalh re-evaluate runs/<run_id> --add answer_quality  # add a new evaluator, keep the rest
```

Reads `traces.jsonl` from disk; no system call is made. Cheap and deterministic for everything except `llm_judge` (which is stochastic). Produces a sibling result file `results.<timestamp>.jsonl`.

### `evalh retry-failed <run_dir>`

Re-run only cases that errored. Lands in v0.2.

```bash
evalh retry-failed runs/<run_id> --max-attempts 3
```

Picks the cells where `trace.error` is set, re-runs the system for those cases only, appends to `traces.jsonl`. Useful after a transient outage.

---

## Output: `runs/<run_id>/`

Every run produces exactly this layout. **The shape is a committed contract.**

```text
runs/2026-05-08T14-22-00_listing_price_eval/
├── config.yaml         # exact config used (env vars masked); reproducer
├── config_hash.txt     # SHA256 of config.yaml; lets you detect drift across runs
├── traces.jsonl        # one Trace per (case × variant), append-only during the run
├── results.jsonl       # one EvaluationResult per (case × variant × evaluator)
├── summary.yaml        # aggregate: per-variant pass-rate, latency, cost, comparison
└── artifacts/          # filesystem-eval artifacts (only if WorkspaceAdapter was used)
    └── <case_id>/<variant_name>/{before/,after/,diff.txt}
```

### Reading `summary.yaml`

The interesting parts to look at, in order of usefulness:

```yaml
variants:                    # per-variant rollup
  - name: agent_main
    cases_total: 100
    cases_passed: 82
    cases_errored: 2
    pass_rate: 0.82
    avg_latency_ms: 3100
    avg_cost_usd: 0.031

by_evaluator:                # which evaluator is the bottleneck?
  - evaluator: answer_quality
    by_variant:
      agent_main: { pass_rate: 0.78, avg_score: 3.9 }
      agent_experimental: { pass_rate: 0.85, avg_score: 4.2 }

comparison:                  # per-case flips against baseline_variant
  baseline: agent_main
  deltas:
    - variant: agent_experimental
      pass_rate_delta: +0.07
      regressions: [listing_price_007]            # was passing on main, failing now
      improvements: [listing_price_002, ...]      # was failing on main, passing now
```

**The first thing to look at after a comparison run is `comparison.deltas[].regressions`.** That's the list of cases the new variant broke. The pass-rate delta alone hides cases that improved *and* regressed.

### Reading `traces.jsonl`

One JSON object per line. Each line is a complete `Trace` (see [`DataModel.md`](DataModel.md)). To pull one:

```bash
# All traces for one case across variants
jq 'select(.case_id == "listing_price_001")' runs/<run_id>/traces.jsonl

# All errored traces
jq 'select(.error != null)' runs/<run_id>/traces.jsonl

# Per-variant latency distribution
jq -r 'select(.variant_name == "agent_main") | .latency_ms' runs/<run_id>/traces.jsonl | datamash min 1 max 1 mean 1 perc:50 1 perc:95 1
```

### Reading `results.jsonl`

One JSON object per `(case × variant × evaluator)`. Use to find which evaluator failed where:

```bash
jq 'select(.passed == false) | {case_id, variant_name, evaluator, reason}' runs/<run_id>/results.jsonl
```

For `llm_judge` results, `detail.assertions` lists per-assertion verdicts when in `nl_assertions` mode — that's the fastest way to see *which* part of a multi-assertion check failed.

---

## Environment variables

| Variable | Used for |
|---|---|
| `ANTHROPIC_API_KEY` | `llm_judge` with Claude backend, `examples/tiny_demo/agent.py` |
| `OPENAI_API_KEY` | `llm_judge` with OpenAI backend (lands in v1) |
| `LANGFUSE_API_KEY`, `LANGFUSE_HOST` | Langfuse `DatasetAdapter` / `TraceStore` / `TraceEnricher` (v1) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTel `TraceStore` (v1) |
| `EVALH_LOG_LEVEL` | `INFO` (default) / `DEBUG` / `WARNING` |

The CLI never accepts secrets via flags — only env vars (or `${VAR}` expansion in the YAML). This keeps them out of shell history.

---

## Cost guardrails

Stochastic systems and LLM judges cost real money. The harness gives you three knobs:

| Knob | Where | What it does |
|---|---|---|
| `evaluators[].config.cost_limit_usd` | `eval.yaml` per `llm_judge` | Aborts a single judge call if predicted cost exceeds this. |
| `run.cost_limit_usd` (v0.2) | `eval.yaml` | Aborts the whole run when accumulated cost crosses the threshold. Failed cells emit a Trace with `error.type = "cost_limit"`. |
| `dataset.sample: N` | `eval.yaml` | Cap how many cases each run pulls (especially important for online-eval pulled from Langfuse). |

Use all three together: per-judge limit catches one bad case; per-run limit catches a runaway; sampling caps the input.

---

## CI integration

A reference GitHub Actions workflow ships at
[`templates/eval.yml`](../templates/eval.yml). It runs the eval on the PR head
and on `main`, then posts a sticky PR comment with per-variant pass rate and
delta vs `main`. The full recipe — wiring, secrets, caching `main` runs, and
cost considerations — lives in [CI.md](CI.md).

---

## Running multiple evals in parallel

Each `evalh run` is its own process. To run several at once, just launch several shells:

```bash
# In CI, simplest form:
evalh run configs/listing_price.yaml &
evalh run configs/coding_agent.yaml &
evalh run configs/pricing_quality.yaml &
wait
```

Each run gets its own `runs/<run_id>/` directory. They don't share anything. If two evals share a system endpoint, scale `max_concurrency` down per-run so you don't overload it.

For coordinated runs (one shared LLM-judge budget across many evals, or one summary report aggregating many runs), wait for v0.2's "run group" feature — not yet implemented.
