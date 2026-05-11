# Repository Structure

Two layouts to know:
- **A. This package's repo** — what `eval-harness` itself looks like.
- **B. A consumer's repo** — what *your* project looks like after you `pip install eval-harness` (or `uv add` / `poetry add`).

---

## A. The `eval-harness` package repo (this one)

Boring, deterministic, easy to grep.

```text
eval-harness/
├── README.md                         # the map
├── PRD.md
├── Architecture.md
├── DataModel.md
├── ConfigSchema.md
├── Adapters.md
├── Evaluators.md
├── Variants.md
├── Filesystem.md
├── Concurrency.md
├── RepositoryStructure.md
├── Roadmap.md
│
├── pyproject.toml
├── uv.lock
├── .python-version
├── .gitignore
├── .github/
│   └── workflows/
│       └── ci.yml
│
├── eval_harness/                     # importable package
│   ├── __init__.py
│   │
│   ├── cli/
│   │   ├── __init__.py
│   │   ├── main.py                   # `evalh` entry point
│   │   ├── commands/
│   │   │   ├── run.py                # `evalh run <config.yaml>`
│   │   │   ├── re_evaluate.py        # `evalh re-evaluate <run_dir>`
│   │   │   ├── compare.py            # `evalh compare <run_a> <run_b>`
│   │   │   └── inspect.py            # `evalh inspect <run_dir> --case <id>`
│   │
│   ├── core/
│   │   ├── __init__.py
│   │   ├── models.py                 # EvalCase, RunVariant, Trace, EvaluationResult, RunSummary, FilesystemArtifact
│   │   ├── config.py                 # EvalConfig + sub-schemas (Pydantic)
│   │   ├── config_loader.py          # YAML → EvalConfig with env-var expansion
│   │   ├── plan.py                   # RunPlan: cases × variants + built adapters
│   │   ├── registry.py               # generic registry used by every factory
│   │   ├── errors.py                 # ConfigError, AdapterError, RetriableError, ...
│   │   └── time.py                   # monotonic helpers, run_id generation
│   │
│   ├── runner/
│   │   ├── __init__.py
│   │   ├── run_eval.py               # the async runner (boring)
│   │   ├── plan_builder.py           # EvalConfig → RunPlan
│   │   ├── retry.py                  # with_retry helper
│   │   └── summary.py                # RunSummary.from_outcomes, ComparisonReport
│   │
│   ├── adapters/
│   │   ├── __init__.py
│   │   │
│   │   ├── system/
│   │   │   ├── __init__.py           # registers built-ins
│   │   │   ├── base.py               # SystemAdapter Protocol
│   │   │   ├── http_adapter.py
│   │   │   ├── python_function_adapter.py
│   │   │   ├── cli_adapter.py        # v0.1
│   │   │   ├── git_branch_adapter.py # v1
│   │   │   └── docker_adapter.py     # v1
│   │   │
│   │   ├── dataset/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── yaml_dataset_adapter.py
│   │   │   ├── jsonl_dataset_adapter.py   # v0.1
│   │   │   ├── postgres_dataset_adapter.py # v1
│   │   │   └── langfuse_dataset_adapter.py # v1
│   │   │
│   │   ├── trace/
│   │   │   ├── __init__.py
│   │   │   ├── base.py
│   │   │   ├── local_files_store.py
│   │   │   ├── sqlite_store.py       # v0.1
│   │   │   ├── postgres_store.py     # v1
│   │   │   ├── langfuse_store.py     # v1
│   │   │   └── arize_store.py        # v1
│   │   │
│   │   └── workspace/
│   │       ├── __init__.py
│   │       ├── base.py
│   │       ├── tempdir_snapshot_adapter.py   # v0; the no-git path
│   │       ├── git_workspace_adapter.py      # v0.1
│   │       └── docker_volume_adapter.py      # v1
│   │
│   ├── evaluators/
│   │   ├── __init__.py
│   │   ├── base.py                   # Evaluator Protocol + base class
│   │   ├── contains_text.py
│   │   ├── tool_called.py
│   │   ├── llm_judge.py
│   │   ├── exact_match.py
│   │   ├── schema_match.py           # v0.1
│   │   ├── latency_under.py          # v0.1
│   │   ├── cost_under.py             # v0.1
│   │   ├── git_diff.py               # v1
│   │   ├── command.py                # v1
│   │   └── semantic_similarity.py    # v1
│   │
│   ├── factories/
│   │   ├── __init__.py
│   │   ├── system_adapter_factory.py
│   │   ├── dataset_adapter_factory.py
│   │   ├── trace_store_factory.py
│   │   ├── workspace_factory.py
│   │   └── evaluator_factory.py
│   │
│   └── reports/
│       ├── __init__.py
│       ├── summary_writer.py         # writes summary.yaml
│       ├── comparison_writer.py      # baseline diff
│       └── markdown_writer.py        # human-friendly markdown report (v0.1)
│
├── configs/                          # user-authored eval configs
│   ├── listing_price_eval.yaml
│   ├── coding_agent_eval.yaml
│   └── examples/
│       └── ...
│
├── datasets/                         # user-authored cases
│   ├── listing_price/
│   │   └── cases.yaml
│   └── coding_agent/
│       └── cases.yaml
│
├── runs/                             # output; one subfolder per run
│   └── .gitkeep
│
├── examples/                         # end-to-end sample evals (committed)
│   ├── listing_price/
│   │   ├── eval.yaml
│   │   ├── cases.yaml
│   │   └── README.md
│   └── coding_agent/
│       ├── eval.yaml
│       ├── cases.yaml
│       ├── fixture_repo/             # initial state for the agent to modify
│       │   └── ...
│       └── README.md
│
└── tests/
    ├── conftest.py
    ├── unit/
    │   ├── test_config_loader.py
    │   ├── test_plan_builder.py
    │   ├── test_runner.py
    │   ├── test_evaluators/
    │   │   ├── test_contains_text.py
    │   │   ├── test_tool_called.py
    │   │   └── test_llm_judge.py
    │   └── test_adapters/
    │       ├── test_http_adapter.py
    │       └── test_tempdir_snapshot.py
    ├── integration/
    │   ├── test_full_run_local_files.py
    │   ├── test_filesystem_eval.py
    │   └── test_variant_comparison.py
    └── fixtures/
        ├── eval_minimal.yaml
        ├── cases_minimal.yaml
        └── repos/
            └── pricing_fixture/
```

---

## Per-package responsibility (one sentence each)

| Package | Job |
|---|---|
| `eval_harness.cli` | Parse argv. Call `runner.run_eval`. Pretty-print exit. |
| `eval_harness.core` | Types, config schema, registry, errors. No I/O. |
| `eval_harness.runner` | Order of operations. Async coroutine. No domain knowledge. |
| `eval_harness.adapters.system` | Talk to systems under test. |
| `eval_harness.adapters.dataset` | Load `EvalCase`s. |
| `eval_harness.adapters.trace` | Persist traces, results, summaries. |
| `eval_harness.adapters.workspace` | Prepare / snapshot / cleanup filesystems. |
| `eval_harness.evaluators` | Read traces, emit `EvaluationResult`s. |
| `eval_harness.factories` | Map config dicts to adapter / evaluator instances. |
| `eval_harness.reports` | Format the run output for humans. |

If a file in `runner/` imports from `requests`, `git`, or `openai`, the design is broken. Move it to an adapter.

---

## Files outside the package

| Path | Purpose |
|---|---|
| `configs/` | Where users put their `eval.yaml` files. Not packaged. |
| `datasets/` | Where users put their `cases.yaml` files. Not packaged. |
| `runs/` | Output directory. `.gitignore`d except for `.gitkeep`. |
| `examples/` | Committed reference evals: `tiny_demo/` (self-contained smoke test) and `listing_price/` (realistic-shape reference, needs a real agent). |
| `tests/` | Unit + integration tests. Fixtures live under `tests/fixtures/`. |

---

## What lives in `pyproject.toml`

The actual file is at the repo root — see [`pyproject.toml`](pyproject.toml). Sketch:

```toml
[project]
name = "eval-harness"
version = "0.0.1"
requires-python = ">=3.11"

# Core deps only — what the runner, registry, factories, built-in adapters,
# and built-in deterministic evaluators import. NO LLM SDKs here.
dependencies = [
  "pydantic>=2",
  "pyyaml",
  "httpx",
  "click",         # for the CLI
  "rich",          # for human-readable output
  "jsonpath-ng",   # for response_mapping JSONPaths
]

[project.optional-dependencies]
# LLM-judge backends — install at least one to use `llm_judge`.
anthropic = ["anthropic>=0.40"]      # provides claude-* models
openai    = ["openai>=1.40"]         # judge support lands when implemented

# Storage / workspace / observability backends.
sqlite   = ["aiosqlite"]
postgres = ["asyncpg"]
langfuse = ["langfuse"]
git      = ["pygit2"]
docker   = ["docker"]
otel     = ["opentelemetry-sdk", "opentelemetry-exporter-otlp"]

[project.scripts]
evalh = "eval_harness.cli.main:cli"

# (entry-point groups for system_adapters, evaluators, dataset_adapters,
# trace_stores, workspaces — see the actual pyproject.toml for the full list)
```

Optional-deps means a user installing `pip install eval-harness` does not pull in any LLM SDK, `pygit2`, or `docker`. They opt into the backends they need:

```bash
pip install 'eval-harness[anthropic]'                # llm_judge with Claude
pip install 'eval-harness[anthropic,langfuse,otel]'  # judge + obs platform mirror
```

**Why no LLM SDK in core?** Eval Harness's runner, factories, and built-in deterministic evaluators (`contains_text`, `tool_called`, `exact_match`) do not import any LLM client. Only `llm_judge` needs one — and which one depends on which model the user picks. Forcing every install to pull `anthropic` would be wrong; forcing it to pull `anthropic` AND `openai` AND `gemini` AND ... would be ridiculous. Optional extras are the right shape.

The entry-points are the canonical extension API. Third-party packages register their adapters/evaluators the same way the built-ins do.

---

## Naming conventions

- Modules are `snake_case`. Classes are `PascalCase`. Type aliases are `PascalCase`.
- Adapter classes end in `Adapter` (`HttpSystemAdapter`, not `HttpSystem`).
- Evaluator classes end in `Evaluator`.
- Stores end in `Store`.
- Factories end in `Factory`.
- Pydantic models live in `core/models.py`. They are imported, never re-defined.
- Config files: `eval.yaml`, `cases.yaml`. Always those names. Never `config.yaml` or `dataset.yaml`.
- Run IDs: `{ISO8601}_{eval_name}` — e.g. `2026-05-03T10-30-00_listing_price_eval`. Sortable.

---

## What goes where: quick decision tree

```
"It talks to the outside world."        → adapter
"It judges a trace."                    → evaluator
"It builds an instance from a dict."    → factory
"It defines a type."                    → core/models.py
"It validates config."                  → core/config.py + factory
"It runs every case."                   → runner (and only the runner)
"It formats a report."                  → reports
"It is a CLI command."                  → cli/commands/
```

When in doubt, it does not belong in `runner/`.

---

## B. A consumer's repo (your project that uses `eval-harness`)

After you install `eval-harness` (`pip install eval-harness`, `uv add eval-harness`, `poetry add eval-harness`), your repo looks roughly like this. Nothing here is enforced; it's a recommended layout.

```text
your-agent-project/
├── pyproject.toml                    # deps: ["eval-harness>=0.0.1"]
├── README.md
├── .github/
│   └── workflows/
│       └── eval.yml                  # run evals on PR; comment summary
│
├── src/
│   └── your_agent/                   # your system under test
│       ├── __init__.py
│       ├── app.py                    # FastAPI/uvicorn entry, if HTTP
│       ├── tools/
│       │   ├── get_listing_details.py
│       │   └── get_average_suburb_price.py
│       └── eval_extensions/          # your custom evaluators / adapters (Python code)
│           ├── __init__.py
│           └── sql_equivalent.py     # registered via entry-point
│
└── evals/                            # all eval-related content lives here
    ├── configs/                      # your eval.yaml files
    │   ├── listing_price.yaml
    │   ├── pricing_quality.yaml
    │   └── coding_agent.yaml
    │
    ├── datasets/                     # your cases.yaml files
    │   ├── listing_price/
    │   │   └── cases.yaml
    │   ├── pricing_quality/
    │   │   └── cases.yaml
    │   └── coding_agent/
    │       ├── cases.yaml
    │       └── fixture_repo/         # if you run filesystem evals
    │           └── ...
    │
    └── runs/                         # eval output; .gitignore'd
        ├── .gitkeep
        └── 2026-05-03T10-30-00_listing_price_eval/
            ├── config.yaml
            ├── traces.jsonl
            ├── results.jsonl
            └── summary.yaml
```

Why nest under `evals/`:
- Keeps `configs/` / `datasets/` / `runs/` from cluttering the project root and from colliding with similarly-named folders your application may already own.
- Makes "what is this project's eval setup" answerable by reading one folder.
- The Python custom-evaluator code lives separately under `src/your_agent/eval_extensions/` — different concern (executable code vs. data/config), different name to keep the distinction sharp.

### Your `pyproject.toml`

```toml
[project]
name = "your-agent"
dependencies = [
  "eval-harness>=0.0.1",
  # plus your agent's deps
]

# Optional: register custom adapters / evaluators so eval.yaml can reference them by name.
[project.entry-points."eval_harness.evaluators"]
sql_equivalent = "your_agent.eval_extensions.sql_equivalent:SqlEquivalentEvaluator"

[project.entry-points."eval_harness.system_adapters"]
your_internal_protocol = "your_agent.eval_extensions.adapters:InternalProtocolAdapter"
```

After installing your project in editable mode (`pip install -e .`, `uv pip install -e .`), `evalh run evals/configs/listing_price.yaml` finds your registrations automatically through Python's standard entry-point mechanism (the same one pytest, Sphinx, click, and mkdocs use for plugins). **You never fork eval-harness — your code lives in your repo, the package stays in `site-packages`.**

### When you'd actually need a custom extension

The built-in adapters and evaluators cover most agent evals. Reach for the entry-point mechanism when one of these is true:

| Situation | What you write | Why eval-harness can't ship it |
|---|---|---|
| Your agent generates SQL; "correct" means returning the same rowset, not string-equal text | Custom `Evaluator` that runs both queries against a fixture DB and diffs results | We don't know your dialect, your schema, or your fixtures |
| Your dataset lives in Snowflake / an internal warehouse / a private labeling tool | Custom `DatasetAdapter` that queries it and maps rows to `EvalCase` | We don't know your schema; your credentials don't belong in our package |
| Your system isn't HTTP — it's gRPC, a queue (SQS/SNS), or an internal RPC protocol | Custom `SystemAdapter` for that protocol | Many protocols are proprietary |
| Your endpoint requires mTLS / SigV4 / internal IAM tokens | Thin `SystemAdapter` that wraps the HTTP one with your auth layer | Auth schemes are organization-specific |
| Your team has a private observability platform (internal events bus, custom backend) | Custom `TraceStore` sink | We don't know your platform's API |
| Compliance check: PII leakage, brand voice, regulatory disclosures | Custom `Evaluator` that calls your existing compliance library | Your compliance library is yours; we can't take a dep on it |

The pattern: **proprietary, organization-specific, or domain-specific extension** that wouldn't make sense to publish as part of a general-purpose package.

### Reading the entry-point string

`your_agent.eval_extensions.sql_equivalent:SqlEquivalentEvaluator` decodes as:

```
your_agent.eval_extensions.sql_equivalent : SqlEquivalentEvaluator
└────────┘ └─────────────┘ └────────────┘   └──────────────────┘
    │             │              │                   │
    │             │              │                   └─ class to instantiate
    │             │              └─ Python file: sql_equivalent.py
    │             └─ subfolder: src/your_agent/eval_extensions/
    └─ your project's importable Python package
```

The colon separates the module path from the class name. Two artifacts in your project:

```python
# src/your_agent/eval_extensions/sql_equivalent.py
class SqlEquivalentEvaluator(Evaluator):
    type = "sql_equivalent"
    async def evaluate(self, case, trace, artifact):
        ...
```

```yaml
# evals/configs/your_eval.yaml
evaluators:
  - name: query_correctness
    type: sql_equivalent              # matches the entry-point key
    config:
      reference_sql: "SELECT id FROM listings WHERE suburb='Richmond'"
```

eval-harness scans the `eval_harness.evaluators` entry-point group at startup, finds `sql_equivalent`, imports the class, and registers it. The runner uses it like any built-in.

### CI integration

```yaml
# .github/workflows/eval.yml — sketch only
name: evals
on: { pull_request: ~ }
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -e .
      - run: evalh run evals/configs/listing_price.yaml
      - run: evalh compare evals/runs/<this-run> evals/runs/<main-baseline>
```

The package CLI does the work. Your project owns its configs, its dataset, and its custom registrations.
