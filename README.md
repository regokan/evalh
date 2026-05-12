# eval-harness

[![CI](https://github.com/regokan/evalh/actions/workflows/ci.yml/badge.svg)](https://github.com/regokan/evalh/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/eval-harness.svg)](https://pypi.org/project/eval-harness/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)

**Run your AI system against a dataset. Capture traces. Score with evaluators. Compare runs. Ship.**

eval-harness is a config-driven evaluation framework for AI systems — agents, RAG pipelines, code-modifying tools, multi-turn assistants, raw LLM endpoints. You describe the run in one YAML file. The harness dispatches the matrix of cases × variants, captures structured traces, runs evaluators, persists results, and produces a comparable summary.

It is not a benchmark suite. It is the harness that runs your benchmarks.

---

## Install

```bash
pip install eval-harness                            # core
pip install 'eval-harness[anthropic]'               # + Claude LLM-judge backend
pip install 'eval-harness[anthropic,langfuse,otel]' # + observability mirrors
pip install 'eval-harness[all]'                     # everything
```

Python 3.11+. Core install pulls only Pydantic v2, httpx, click, rich, jsonpath-ng, pyyaml, python-dotenv, jsonschema, fsspec. LLM SDKs and platform clients ship as optional extras so you only pay for what you use.

---

## 60-second tour

```bash
# 1. Install
pip install 'eval-harness[anthropic]'

# 2. Drop your Anthropic key into the smoke fixture
echo "ANTHROPIC_API_KEY=sk-ant-..." > examples/tiny_demo/.env

# 3. Run
evalh run examples/tiny_demo/eval.yaml

# 4. Inspect a case
evalh inspect runs/<run_id> --case tiny_demo_001

# 5. Compare two runs
evalh compare runs/<run_a> runs/<run_b>
```

That run produces a `runs/<run_id>/` directory with:

```
config.yaml         # exact config used, secrets masked
traces.jsonl        # one Trace per (case, variant)
results.jsonl       # one EvaluationResult per (case, variant, evaluator)
summary.yaml        # per-variant pass-rates + baseline comparison
report.md           # human-readable summary
```

These four files are the durable surface. Everything else (drift reports, inspect output, webhook posts) is derived from them.

---

## What it does

| Concern | What you get |
|---|---|
| **System under test** | Plug in any HTTP service, Python function, CLI subprocess, branch checkout, Docker image, multi-turn user simulator, or replay-from-historical-trace. |
| **Evaluators** | 13 built-ins covering text checks, tool-call assertions, LLM-as-judge (nl-assertions + rubric), schema validation, latency/cost gates, thinking-token rules, semantic similarity, git-diff checks, command exit codes. Plus a clean extension API. |
| **Trace storage** | Local JSONL (default), SQLite, Postgres, OTel collector, Langfuse, Phoenix, Arize, Braintrust, Slack / Discord / Linear webhooks. |
| **Dataset sources** | YAML, JSONL, plus production-traffic pulls from Langfuse, Phoenix, Arize, Helicone, Braintrust (with `embed_full_trace` for replay-style evaluation). |
| **Workspace isolation** | Tempdir snapshot (default), git worktree, Docker volume (sandboxed; can't read host `$HOME`). |
| **Variants** | One run dispatches N × M of cases × system configurations. Use for A/B testing, fleet evals, branch comparison, stochastic sampling. |
| **Distribution** | Local async (default), Ray, Modal, Celery, Kubernetes Jobs. Same code, different executor. |
| **Drift detection** | Promote a run as the baseline; `evalh drift` surfaces regressions vs. baseline. Wire to Slack via webhook on a daily cron. |
| **Reports** | Markdown summary, baseline ComparisonReport, per-evaluator rollup, regressions/improvements case-by-case. |

---

## CLI

```text
evalh run <eval.yaml>                              # execute an eval
evalh run --retry-only-failed <run_dir>            # re-run cells that errored
evalh re-evaluate <run_dir> [--add <evaluator>]    # re-score existing traces offline
evalh inspect <run_dir> [--case <id>] [--failed]   # view a case + its results + filesystem artifacts
evalh compare <run_a> <run_b>                      # diff two runs (regressions / improvements)
evalh promote <run_dir>                            # mark a run as the eval's baseline
evalh drift <run_dir> [--exit-nonzero-on-regression]  # compare against baseline; CI gate
```

---

## eval.yaml in 30 lines

```yaml
eval:
  name: listing_price

dataset:
  type: yaml
  path: cases.yaml

systems:                                   # one entry per variant
  - name: agent_main
    adapter: http
    endpoint: http://localhost:8000/chat
    response_mapping:
      final_answer: $.answer
      tool_calls: $.tool_calls
  - name: agent_experimental
    adapter: http
    endpoint: http://localhost:8000/chat
    query_params: { variant: experimental }
    response_mapping: { final_answer: $.answer, tool_calls: $.tool_calls }

evaluators:
  - name: must_call_listing_tool
    type: tool_called
    config: { tool_name: get_listing_details }
  - name: answer_quality
    type: llm_judge
    config:
      model: claude-4-7
      nl_assertions:
        - "The answer mentions the listing's suburb."
        - "The answer compares the listing price to the suburb average."
      pass_when: all

run:
  max_concurrency: 4
  baseline_variant: agent_main
  cost_limit_usd: 5.00

output:
  - { type: local_files, path: runs/ }
```

See [`docs/ConfigSchema.md`](docs/ConfigSchema.md) for every field.

---

## Examples

Four runnable references live under [`examples/`](examples/):

- **[`tiny_demo/`](examples/tiny_demo/)** — self-contained smoke test against Claude. Needs only `ANTHROPIC_API_KEY`. Finishes in under a minute.
- **[`listing_price/`](examples/listing_price/)** — realistic-shape eval: HTTP agent service, two variants, LLM judge. Plug your service in.
- **[`online_eval/`](examples/online_eval/)** — replay-style evaluation. The fixture adapter ships embedded historical traces; the `replay` SystemAdapter scores them. Swap the fixture for Langfuse / Phoenix / Arize to score production traffic.
- **[`coding_agent/`](examples/coding_agent/)** — workspace-mutating agent. Claude patches a fixture repo; the `command` evaluator runs pytest in the artifact directory.

---

## Distributed runs

The default `LocalExecutor` uses `asyncio.gather` + a semaphore — perfect for thousands of cases on one box. For larger fleets, plug in another executor:

```yaml
run:
  executor:
    type: ray            # or modal, celery, kubernetes
    address: auto        # or your cluster address
    object_store_memory: 2_147_483_648
```

The cell is the unit of distribution. Workers rebuild adapters + evaluators from your `eval.yaml` and the entry-point registry — **config travels, code doesn't**, so your custom evaluators work on Ray workers without pickling pitfalls. See [`docs/Executors.md`](docs/Executors.md).

---

## Custom evaluators

When the built-ins don't cover your domain (e.g., "the SQL the agent generated returns the same rowset as the reference SQL"), write your own and register it via Python entry-points — no fork of eval-harness required:

```toml
# your-package/pyproject.toml
[project.entry-points."eval_harness.evaluators"]
sql_equivalent = "your_package.evaluators.sql_equivalent:SqlEquivalentEvaluator"
```

```yaml
# your eval.yaml
evaluators:
  - name: query_correctness
    type: sql_equivalent
    config: { reference_sql: "SELECT id FROM listings WHERE suburb='Richmond'" }
```

The same extension pattern works for system adapters, dataset adapters, trace stores, workspace adapters, embedder backends, and LLM-judge backends. See [`docs/Evaluators.md`](docs/Evaluators.md) and [`docs/Adapters.md`](docs/Adapters.md).

---

## Observability integrations

eval-harness coexists with your existing observability stack — it doesn't replace it. The local `runs/<run_id>/` directory stays canonical; remote sinks are mirrors. Failed mirror writes don't abort the run, they land in `summary.yaml > sink_errors`.

```yaml
output:
  - { type: local_files, path: runs/ }                                        # canonical
  - { type: otel,        endpoint: "https://api.honeycomb.io" }               # mirror to Honeycomb
  - { type: langfuse,    api_key: "${LANGFUSE_API_KEY}", host: "..." }        # mirror to Langfuse UI
  - { type: webhook,     platform: slack, url: "${SLACK_WEBHOOK_URL}" }       # daily summary post
```

Backends shipped: OTel (Honeycomb / Datadog / Tempo / Grafana / Phoenix-OTLP / self-hosted Langfuse), Langfuse, Phoenix, Arize, Braintrust, Helicone (dataset only), Slack / Discord / Linear (webhook). See [`docs/Observability.md`](docs/Observability.md).

---

## CI integrations

Two reference workflows live under [`templates/`](templates/):

- **[`templates/eval.yml`](templates/eval.yml)** — on every PR, run the eval against the PR head, compare with `main`'s baseline, post a markdown summary back to the PR comments.
- **[`templates/eval-daily.yml`](templates/eval-daily.yml)** — on a schedule (or `workflow_dispatch`), run the eval, compute drift vs. the saved baseline, and post regressions to a webhook channel.

Walkthrough in [`docs/CI.md`](docs/CI.md).

---

## Documentation

| Topic | Doc |
|---|---|
| Why the project exists | [`docs/PRD.md`](docs/PRD.md) |
| End-to-end design | [`docs/Architecture.md`](docs/Architecture.md) |
| Trace / Case / Result / Summary models | [`docs/DataModel.md`](docs/DataModel.md) |
| `eval.yaml` and `cases.yaml` field reference | [`docs/ConfigSchema.md`](docs/ConfigSchema.md) |
| System / Dataset / TraceStore / Workspace / Enricher contracts | [`docs/Adapters.md`](docs/Adapters.md) |
| Built-in evaluators + writing your own | [`docs/Evaluators.md`](docs/Evaluators.md) |
| The variant matrix (A/B, branch, fleet, sampling) | [`docs/Variants.md`](docs/Variants.md) |
| Filesystem artifacts + sandboxed workspaces | [`docs/Filesystem.md`](docs/Filesystem.md) |
| Concurrency model + executor abstraction | [`docs/Concurrency.md`](docs/Concurrency.md) |
| Distributed executors (Ray, Modal, Celery, K8s) | [`docs/Executors.md`](docs/Executors.md) |
| Observability platform integrations | [`docs/Observability.md`](docs/Observability.md) |
| Drift detection + CLI surface | [`docs/CLI.md`](docs/CLI.md) |
| GitHub Actions recipes | [`docs/CI.md`](docs/CI.md) |
| Project layout + plugin packaging | [`docs/RepositoryStructure.md`](docs/RepositoryStructure.md) |
| Milestone-by-milestone history | [`CHANGELOG.md`](CHANGELOG.md), [`docs/Roadmap.md`](docs/Roadmap.md) |

---

## Status

All planned milestones are shipped — v0 through v2. The project covers what the roadmap set out to do and nothing beyond it (hosted SaaS, web dashboard, auth, and built-in dataset libraries are explicitly out of scope; see [`docs/Roadmap.md > Forever-maybe`](docs/Roadmap.md)).

Snapshot: **132 source files · 657+ tests · ruff & mypy --strict clean · 6 adapter families · 5 executor backends · 8 observability platform integrations.**

---

## Contributing

Issues and PRs welcome. See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup, testing, and submission guidelines. The architectural rails the project is built against live under [`.claude/rules/`](.claude/rules/) — read those before substantive PRs.

---

## License

[MIT](LICENSE). Copyright © 2026 eval-harness contributors.
