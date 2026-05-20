# Example: slack_drift_notify

A tiny add-on to [`examples/regression_gate/`](../regression_gate/): same agent, same cases, same evaluators, plus a second `output:` entry of `type: webhook, platform: slack`. The webhook posts the run summary to a Slack channel at finalize time — and when `evalh drift` has written a drift report, the post highlights the regressed case ids and the pass-rate delta.

This is the canonical shape for "I already have a CI eval — how do I get a chat ping when it regresses?". The example is about the `output:` list-of-sinks contract, not about new gating signals: there are zero new evaluators.

> **Deviation from `examples/plan.md`.** The plan describes the webhook config as `type: webhook, format: slack`. The shipped `WebhookTraceStore` takes `platform: slack` (a `format:` dict exists for per-platform options but it is not the dispatch key). The eval below uses the actual API. Same intent, accurate wiring.

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | Identical to [`../regression_gate/eval.yaml`](../regression_gate/eval.yaml) except for the `output:` list — adds a webhook sink alongside `local_files`. |
| Agent + cases | **Not shipped here.** The eval points at [`../regression_gate/agent.py`](../regression_gate/agent.py) and [`../tiny_demo/cases.yaml`](../tiny_demo/cases.yaml) — `regression_gate` itself links to `tiny_demo`'s cases, and forking either would violate convention 8 ("Link, don't duplicate"). |

## Required environment

| Variable | Required? | What it does |
|---|---|---|
| `SLACK_WEBHOOK_URL` | **optional** | When set, the webhook sink POSTs a Slack Block Kit summary at finalize time. When unset, the env-var default expands to `""` and the webhook store short-circuits with a single warning — the run still succeeds and `local_files` is unaffected. |

No install extras are needed for Slack. The webhook store uses `httpx` directly (no SDK). The `[webhook]` extra is only needed for Linear's GraphQL `createComment` path; Slack and Discord are plain HTTPS POSTs.

## Run it

```bash
# Offline — webhook short-circuits with a warning, run still succeeds.
evalh run examples/slack_drift_notify/eval.yaml

# With Slack — set the secret and the same command pings your channel.
export SLACK_WEBHOOK_URL='https://hooks.slack.com/services/T000/B000/xxxx'
evalh run examples/slack_drift_notify/eval.yaml
```

Expected runtime: under 1s for the three shipped cases (the agent is the same deterministic stub regression_gate ships).

## What happens, in order

1. The runner expands `cases × variants` — three cases, one variant.
2. For each case the `python_function` adapter calls the regression_gate stub. Same `tool_called` + `contains_text` evaluators fire.
3. The runner finalizes the run and asks every entry in `output:` to write.
4. `local_files` writes `runs/<run_id>/{config.yaml, traces.jsonl, results.jsonl, summary.yaml}` — the canonical store, identical to regression_gate's output.
5. `webhook` reads the same `RunSummary`:
   - If `SLACK_WEBHOOK_URL` is set, it formats Slack Block Kit JSON and POSTs to the webhook.
   - If `SLACK_WEBHOOK_URL` is unset (URL expands to empty), it logs `webhook trace store (slack): url is empty; skipping summary POST` and returns. The run summary still landed in `local_files`; only the chat side-channel is skipped.

When a regression is present — i.e. `evalh drift` was run between `evalh run` and finalize, or the runner constructed the comparison from a baseline — the Slack message carries the drift section with regressed case ids and the pass-rate delta.

## What the Slack message looks like

### Clean run (no regressions)

The message is a Block Kit payload with a header and a per-variant section:

```
evalh slack_drift_notify — 2026-05-20T15-00_slack_drift_notify

agent_stub
3/3 passed (100.0%)
```

Per variant, one line of pass-count and pass-rate. No drift block — the comparison is `kind='ad_hoc'` (the baseline_variant in this run) and the formatter only renders drift content for `kind='drift'`.

### Regression run (drift block present)

When `evalh drift <run_dir>` has stamped a `comparison.kind='drift'` onto the summary before the webhook fires, a divider and a regression block are appended:

```
evalh slack_drift_notify — 2026-05-20T15-30_slack_drift_notify

agent_stub
1/3 passed (33.3%)

──────────

⚠️ Drift vs `2026-05-19T10-00_slack_drift_notify`
pass-rate Δ: -66.67%    regressions: 2    improvements: 0
regressions: `tiny_demo_001`, `tiny_demo_002`
```

`⚠️` becomes `✅` when `regressions_count == 0` (an improvements-only drift report). The top regressed and improved case ids come from `ComparisonReport.top_regressions` / `top_improvements` — usually a small bounded slice, not the full list, so the Slack message stays readable.

## Why this works

The harness treats `output:` as a list, not a single sink. Every entry implements the same `TraceStore` protocol: `save_summary` is the only hook the webhook store cares about (webhooks are inherently summary-grained, not per-cell), but it's the same method `local_files`, `sqlite`, and the platform stores all implement. That symmetry is what makes "add Slack" a one-line config change instead of a runner change.

Two-sink output means the chat ping happens regardless of whether downstream gates (e.g. `evalh drift --exit-nonzero-on-regression`) pass or fail. Slack sees the summary either way; the CI job's pass/fail is decided separately by the drift exit code, and any webhook failure is collected into `RunSummary.sink_errors` rather than failing the whole run (multi-sink failure-soft, per the trace-store contract).

The offline path — empty `${SLACK_WEBHOOK_URL:-}` → no HTTP, just a warning — is what lets a CI workflow ship one `eval.yaml` to both a forked PR (no secret access) and the main repo (secret present), without conditional config templating.

## Extending it

- **Discord instead.** Change `platform: slack` to `platform: discord` and point `url` at `${DISCORD_WEBHOOK_URL:-}`. The formatter produces a Discord embed; the empty-URL short-circuit behaves identically.
- **Linear comments.** `platform: linear` + `api_key: ${LINEAR_API_KEY}` + `issue_id`. Requires `pip install 'eval-harness[webhook]'` for the GraphQL SDK.
- **Pin to regressions only.** Drop this sink and instead trigger Slack from the GitHub workflow on `evalh drift` non-zero exit. The webhook sink fires *every* run by design; if your channel hates that, gate from CI instead.
- **Tighter regression block.** The `top_regressions` slice is bounded by the runner; if you need a longer list, edit the `format_slack` formatter in `eval_harness/_platforms/webhook.py` rather than the example.
