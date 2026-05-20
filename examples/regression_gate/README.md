# Example: regression_gate

The CI / drift workflow end-to-end: `evalh run` → `evalh promote` → `evalh drift`, and a `.github/workflows/eval.yml` snippet that turns the last step into a PR gate. This is the *operational* story of the harness — the thing teams care about once they have a working eval.

Runs offline. No API key, no network. The agent is a deterministic stub so the example is reproducible on a fresh checkout; the focus is the run lifecycle, not the agent.

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | One variant, deterministic evaluators, dataset linked from [`../tiny_demo/cases.yaml`](../tiny_demo/cases.yaml). |
| [`agent.py`](agent.py) | Deterministic stub. Returns canned answers driven by `case["metadata"]`. ~30 lines. |
| [`baseline/`](baseline/) | A frozen prior run committed to the repo — the reference point for `evalh drift`. |

## Required environment

None. Runs offline.

## Run it

```bash
evalh run examples/regression_gate/eval.yaml
```

Expected: all three cases pass, single variant `agent_stub`.

## The five-step walkthrough

### 1. Establish a baseline

```bash
evalh run examples/regression_gate/eval.yaml
# -> runs/2026-..._regression_gate/
```

### 2. Promote it

```bash
evalh promote runs/2026-..._regression_gate
```

This drops a symlink at `runs/baselines/regression_gate/` pointing at the run. `evalh drift` reads it as the comparison reference.

### 3. Introduce a regression

Apply this one-line patch to [`agent.py`](agent.py) — drop the suburb from the answer:

```diff
-    answer = (
-        f"Listing {listing_id} is in {suburb}. The {suburb} average is "
-        f"${suburb_avg:,}, and the listing is priced at ${price:,} — "
-        f"{comparison} the suburb average."
-    )
+    answer = (
+        f"Listing {listing_id}: priced at ${price:,}, average ${suburb_avg:,} "
+        f"({comparison} the average)."
+    )
```

This breaks the `answer_mentions_suburb` evaluator for all three cases — no suburb name in the output.

### 4. Run + drift

```bash
evalh run examples/regression_gate/eval.yaml
evalh drift runs/2026-..._regression_gate --exit-nonzero-on-regression
echo "exit code: $?"
```

The drift report names the regressed cases, writes `drift.yaml` next to the run, and exits non-zero.

### 5. Gate a PR on it

```yaml
# .github/workflows/eval.yml
name: eval
on: [pull_request]
jobs:
  drift-gate:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install eval-harness
      - name: Run eval
        run: evalh run examples/regression_gate/eval.yaml
      - name: Drift gate
        run: |
          RUN_DIR=$(ls -td runs/*_regression_gate | head -1)
          evalh drift "$RUN_DIR" \
            --baseline examples/regression_gate/baseline \
            --exit-nonzero-on-regression
```

The job fails iff `evalh drift` reports a regression case. Approvals + merges follow whatever policy your repo uses for failing checks.

## Regenerating the committed baseline

The fixture under [`baseline/`](baseline/) is committed so the example is self-contained on a fresh checkout. To regenerate it after an intentional behaviour change to the stub:

```bash
evalh run examples/regression_gate/eval.yaml
RUN_DIR=$(ls -td runs/*_regression_gate | head -1)
cp "$RUN_DIR"/{summary.yaml,results.jsonl,traces.jsonl,config.yaml} \
   examples/regression_gate/baseline/
```

The `config.yaml` snapshot is included so `evalh drift`'s baseline reader sees the same `eval.name`.

## Why this works

The drift report is a diff of two `results.jsonl` files keyed by `case_id`. As long as the baseline contains the same evaluator names and the same case ids, the comparison is well-defined — the baseline doesn't need to come from the same git revision, and there's no embedded model identity to match.

That's what makes this a generic CI gate: any later run against any later code can drift-compare against the committed baseline, and the workflow fails the moment a previously-passing case fails.

## Extending it

- **Swap the agent.** Point `systems[0].target` at your own `async def run(case, variant)`. The baseline regenerates from the next `evalh run`.
- **Slack on regression.** Add a second `output:` of `type: webhook` and the same drift step posts to a channel. See the proposal in [`examples/plan.md`](../plan.md) (Tier 3.10).
- **Compare any two runs.** `evalh compare <a> <b>` ignores baselines entirely — handy for inspecting per-commit deltas without promoting anything.
