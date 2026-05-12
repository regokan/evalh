# CI integration

> A reference recipe for running eval-harness on pull requests and posting a sticky comment with pass-rate + delta vs `main`.

The package ships a copy-paste GitHub Actions workflow at
[`templates/eval.yml`](../templates/eval.yml). It is **not** auto-installed —
copy it into your own agent repo at `.github/workflows/eval.yml` and adapt the
`EVAL_CONFIG` path. There is no separate action distribution layer; the
workflow uses standard `actions/*` plus `gh` from the runner.

---

## What it does

```mermaid
flowchart LR
    PR[pull_request opened/synced] --> A[checkout PR head]
    PR --> B[checkout main]
    A --> I1[pip install -e]
    B --> I2[pip install -e]
    I1 --> R1[evalh run on PR config]
    I2 --> R2[evalh run on main config]
    R1 --> M[build markdown body<br/>per-variant pass rate + Δ vs main]
    R2 --> M
    M --> C{existing<br/>evalh comment?}
    C -- yes --> P[PATCH that comment via gh api]
    C -- no --> N[POST new comment via gh pr comment]
```

One sticky comment per PR. Subsequent CI runs update that comment in place
instead of stacking. The HTML sentinel `<!-- evalh-summary -->` at the top of
the body is how the workflow finds its own previous comment.

---

## Prerequisites

Add these in your repo's **Settings → Secrets and variables → Actions**:

| Secret | Required when |
|---|---|
| `ANTHROPIC_API_KEY` | Your eval or judge uses Anthropic |
| `OPENAI_API_KEY` | Your eval or judge uses OpenAI (once the backend ships) |
| `AGENT_API_KEY` | Your system-under-test is an authenticated HTTP service and the YAML references `${AGENT_API_KEY}` |
| `GITHUB_TOKEN` | Provided automatically by Actions; no setup required |

The workflow expects your `eval.yaml` to reference these as `${VAR}` placeholders — eval-harness's config loader expands them at load time.

---

## Wiring it into a typical agent repo

The reference workflow assumes the repo layout from
[RepositoryStructure.md](RepositoryStructure.md):

```text
your-agent/
├── .github/workflows/eval.yml          # copy of templates/eval.yml
├── pyproject.toml                       # depends on eval-harness[anthropic]
├── src/your_agent/
└── evals/
    ├── configs/listing_price.yaml       # set EVAL_CONFIG to this
    ├── datasets/listing_price/cases.yaml
    └── runs/                            # gitignored; CI artifacts land here
```

One env-var change in the workflow points at your eval:

```yaml
env:
  EVAL_CONFIG: evals/configs/listing_price.yaml
  RUNS_DIR: evals/runs
```

The PR run and the main baseline run each get their own checkout under `pr/`
and `base/` so there is no working-tree collision. Both runs install
eval-harness into the same Python interpreter (Python's import system handles
the editable swap correctly because the workflow installs each in turn before
each `evalh run`).

---

## The comparison-against-main pattern

The reference workflow runs `main` from scratch on every PR. That is the
simplest correct thing — `main` and the PR run on the same runner with the
same SDK versions, so any differences are real. It is also the most expensive
thing.

Two cheaper patterns, in order of usefulness:

### Cache the latest main run

```yaml
- name: Restore main run cache
  id: cache-main
  uses: actions/cache@v4
  with:
    path: base/${{ env.RUNS_DIR }}
    key: evalh-main-${{ hashFiles('pr/evals/configs/**', 'pr/evals/datasets/**') }}

- name: Run eval on main (baseline)
  if: steps.cache-main.outputs.cache-hit != 'true'
  working-directory: base
  run: evalh run "$EVAL_CONFIG"
```

The cache key includes the configs/datasets so a config change invalidates the
cache. The system under test isn't in the key — it can drift; that's exactly
what the eval is supposed to catch.

### Schedule the main run separately

```yaml
on:
  schedule:
    - cron: "0 6 * * *"     # daily at 06:00 UTC
  workflow_dispatch:        # manual trigger
```

Stash the resulting `evals/runs/<run_id>/` somewhere durable (an S3 bucket, a
release artifact, a separate Pages-deploy branch) and have the PR workflow
fetch it instead of running main itself. Pairs well with `evalh compare`.

---

## Cost considerations

Stochastic evals over LLMs cost real money. Out-of-the-box defaults that
matter for CI:

| Knob | Where | Effect |
|---|---|---|
| `dataset.sample: N` | `eval.yaml` | Cap how many cases CI runs. Use a small sample (10–50) for PR runs and a larger one for the scheduled main run. |
| `evaluators[].config.cost_limit_usd` | `eval.yaml` per `llm_judge` | Aborts a single judge call if predicted cost exceeds. Catches runaway prompts. |
| `run.cost_limit_usd` | `eval.yaml` (v0.2) | Aborts the whole run when accumulated spend crosses the threshold. Failed cells emit `error.type = "cost_limit"`. |
| `run.max_concurrency` | `eval.yaml` | Throttle concurrent system calls. Keep low if the system has a real rate limit. |

In practice: a daily scheduled main run on the full dataset + PR runs on a
sampled subset (`dataset.sample: 25`) is a reasonable starting point. Tune
upward when the eval becomes the gating signal for merging.

---

## What the comment looks like

```markdown
<!-- evalh-summary -->
## eval-harness

Config: `evals/configs/listing_price.yaml` (PR `1a2b3c4d` vs main `9f8e7d6c`)

| variant | PR pass rate | main pass rate | Δ |
|---|---|---|---|
| `agent_main` | 88.0% (22/25) | 92.0% | -4.0pp |
| `agent_experimental` | 96.0% (24/25) | 88.0% | +8.0pp |

_Run dir (PR): `2026-05-12T...`_   _Run dir (main): `2026-05-12T...`_
```

Numbers come straight from `summary.yaml` on each side — no re-evaluation, no
extra Python deps beyond what `eval-harness` already pulls in.

---

## Gating PR merges on eval results

The reference workflow does **not** fail the job on regressions. It posts the
delta and lets the reviewer decide. If you want hard gating, add a step that
parses the same `summary.yaml` and `exit 1`s when a pass rate drops more than
some threshold. Keep the gating policy in your repo, not in eval-harness —
acceptable regression varies wildly across teams.

`evalh compare` (informational, exits 0 today) will grow a `--fail-on-regression`
flag in v0.2; until then, an inline Python check is the right shape.

---

## Scheduled runs with drift alerts

The PR-comment workflow above is *event-driven*: it fires when a contributor
opens or updates a pull request. A different question — "did anything regress
overnight without anyone shipping a change?" — needs a *time-driven* workflow.
`templates/eval-daily.yml` is the recipe for that loop.

```mermaid
flowchart LR
    CRON[cron: daily] --> R[evalh run]
    R --> RUN[runs/&lt;new&gt;/]
    RUN --> D[evalh drift]
    BASE[runs/baselines/&lt;eval&gt;/<br/>symlink, from `evalh promote`] -. read .-> D
    D --> Y[runs/&lt;new&gt;/drift.yaml]
    R -. webhook sink in output: .-> CHAT[Slack / Discord / Linear]
    D -- exit 1 on regression --> FAIL[GitHub job fails<br/>+ artifact uploaded]
```

### What's involved

- **`evalh drift`** ([CLI.md → Drift detection](CLI.md#drift-detection)) reads
  the baseline symlink at `runs/baselines/<eval_name>/`, compares the new run
  to it via the shared delta primitives, and writes a structured
  `ComparisonReport(kind='drift')` to `runs/<id>/drift.yaml`. The
  `--exit-nonzero-on-regression` flag fails the GitHub job when any
  regression case is present — the drift.yaml is still persisted on the
  failure path so the run dir always carries the report.
- **Webhook TraceStore** (a `type: webhook` entry in `eval.yaml > output:`)
  POSTs a summary message to Slack / Discord / Linear at run finalize time.
  When the run's summary carries `comparison.kind='drift'`, the webhook
  message highlights the top regression case IDs and the pass-rate Δ.
  Slack + Discord use plain HTTPS POST (no SDK); Linear needs the
  `[webhook]` extra for its GraphQL `createComment` SDK call.
- **`evalh promote`** ([CLI.md → Drift detection](CLI.md#drift-detection))
  designates a run as the new baseline: an atomic symlink at
  `runs/baselines/<eval_name>/`. Promote when a run is green and you want
  tomorrow's drift report measured against it.

### Recipe

The full workflow is in [`templates/eval-daily.yml`](../templates/eval-daily.yml).
Copy it into your repo at `.github/workflows/eval-daily.yml` and adapt the
`EVAL_CONFIG`, `RUNS_DIR`, and chat-tool secret. The skeleton:

```yaml
on:
  schedule:
    - cron: "0 7 * * *"     # 07:00 UTC daily
  workflow_dispatch:

jobs:
  eval:
    runs-on: ubuntu-latest
    env:
      SLACK_WEBHOOK_URL: ${{ secrets.SLACK_WEBHOOK_URL }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.13" }
      - run: pip install "eval-harness[anthropic,webhook]"
      - run: evalh run evals/configs/myeval.yaml
      - run: evalh drift "$NEW_RUN" --exit-nonzero-on-regression
```

…where the `eval.yaml` carries the webhook sink alongside `local_files`:

```yaml
output:
  - type: local_files
    path: evals/runs/
  - type: webhook            # secondary sink — failure-soft via RunSummary.sink_errors
    platform: slack
    url: ${SLACK_WEBHOOK_URL}
```

Two-sink output means the chat ping happens regardless of whether the drift
step fails — Slack sees the run summary, and the GitHub job failure surfaces
the regression separately. If you want chat to ping ONLY on regression, drop
the webhook sink and rely on GitHub's standard failure notifications.

### Tuning

- **Cron cadence**: `"0 7 * * *"` for daily; `"0 7 * * 1"` for Monday-only.
  Mind your team's working hours.
- **Concurrency**: `cancel-in-progress: false` (the template's default) so
  the previous day's run can finish before today's starts.
- **Promotion cadence**: a green daily run isn't automatically promoted — the
  drift baseline only moves when you run `evalh promote runs/<id>`. Typical
  flow: a release-cut workflow promotes the post-release run as the new
  baseline; nightly drift then measures against that release.

### Out of scope (today)

- Auto-promotion on green runs. Promotion is a deliberate human decision
  in v1.x; the drift CLI's structured `drift.yaml` is what release tooling
  reads.
- Drift across more than two runs (rolling window). The shared delta
  primitives in `eval_harness.runner._deltas` are pair-shaped today.

---

## Distributed executors in CI

The v2 distributed executors come in four flavours; what CI can run for
you depends on how reachable each transport's runtime is from a GitHub
runner.

| Marker | CI? | How |
|---|---|---|
| `@pytest.mark.ray` | ✓ | `ray` ships an in-process mode — no cluster needed. The `distributed` job in `ci.yml` installs `[ray]` and runs the marker. |
| `@pytest.mark.celery` | ✓ | A Redis service container holds the broker; the same job sets `EVALH_TEST_REDIS_URL=redis://localhost:6379/0` and runs the marker. |
| `@pytest.mark.modal` | ✗ | Modal requires a configured account + CLI token. Run locally — see below. |
| `@pytest.mark.kubernetes` | ✗ | Needs a real cluster (kind, minikube, or a remote target). Run locally — see below. |

### Modal — local

```bash
modal token new           # one-time, writes ~/.modal.toml
poetry install --extras modal
EVALH_TEST_MODAL=1 poetry run pytest tests/ -m modal
```

The integration test is shape-only by default — it builds the
`modal.App` + `modal.Function` without spawning a remote call (cost +
deployed-app requirements would dominate). For real spawn coverage,
deploy an app first and adapt the test's `app_name`.

### Kubernetes — local

```bash
kind create cluster --name evalh        # or `minikube start`
poetry install --extras kubernetes
docker build -t local/evalh-worker .    # ENTRYPOINT ["evalh-cell-worker"]
kind load docker-image local/evalh-worker --name evalh
export EVALH_TEST_K8S_CONTEXT=kind-evalh
export EVALH_TEST_K8S_IMAGE=local/evalh-worker
poetry run pytest tests/ -m kubernetes
```

The `EVALH_TEST_K8S_CONTEXT` env var is the opt-in switch the test
gates on — a dev with a stray kubeconfig won't get cluster-touching
tests they didn't ask for. The image must carry eval-harness + your
plugin packages; the test's stub agent module is in
`tests/fixtures/k8s_stub_agent.py` and needs to be importable inside
the pod (build it into the image or mount it as a volume).

---

## Where to go next

- [CLI.md](CLI.md) — full command reference for `evalh run` / `inspect` / `compare` / `promote` / `drift`
- [RepositoryStructure.md](RepositoryStructure.md) — how to lay out a consumer repo so the workflow finds everything
- [ConfigSchema.md](ConfigSchema.md) — `eval.yaml` field reference; `${VAR}` expansion lives here
