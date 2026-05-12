# Config Schema

Two YAML files drive every run:
- `eval.yaml` — describes the run (dataset, systems, evaluators, output)
- `cases.yaml` — the dataset itself

Both are validated at load time. Errors point to the line + key that failed.

The canonical, runnable example for both files lives in [`examples/listing_price/`](../examples/listing_price/) — read those alongside this reference. This document specifies the schema; the example shows it in use.

---

## `eval.yaml`

Working sample: [`examples/listing_price/eval.yaml`](../examples/listing_price/eval.yaml).

### Top-level structure

```text
eval          run identity (name, description, owner, tags)
metadata      free-form fields copied verbatim into summary.yaml
dataset       which DatasetAdapter loads cases, plus filters and sampling
systems       one or more variants — what the runner dispatches in parallel
workspace     optional; required when an evaluator inspects filesystem
trace         optional capture hints to the SystemAdapter
evaluators    one or more evaluators — run against each (case, trace)
pass_criteria optional; combines per-evaluator results into per-case pass/fail
run           runtime knobs (concurrency, retry, baseline)
output        list of TraceStores; first one is canonical, others are mirrors
```

### Field reference

| Path | Type | Required | Notes |
|---|---|---|---|
| `eval.name` | string | yes | Run-id ingredient. |
| `eval.description` | string | no | Free text. |
| `eval.owner` | string | no | Free text; surfaced in reports. |
| `eval.tags` | list[string] | no | Free text; surfaced in reports. |
| `metadata.*` | dict | no | Copied verbatim into `summary.yaml`. Use for system version, prompt version, judge model, etc. |
| `dataset.type` | enum | yes | One of the registered DatasetAdapters. See [Adapters.md](Adapters.md). |
| `dataset.path` | string | type-dependent | Required for `yaml`, `jsonl`. |
| `dataset.filter` | dict | no | Subset by `metadata.<key>`. |
| `dataset.sample` | int | no | Random sample of cases. Combine with `sampling.strategy` for stratified. |
| `systems` | list | yes | At least one entry. Multiple = run matrix. |
| `systems[].name` | string | yes | Unique within run. |
| `systems[].adapter` | enum | yes | One of the registered SystemAdapters. |
| `systems[].provider` | enum | no | HTTP-adapter only. Preset that auto-fills `request_template` and `response_mapping`. See [Adapters.md](Adapters.md). |
| `systems[].response_mapping` | dict[JSONPath] | conditional | How the HTTP adapter extracts trace fields from the response JSON. Recognized keys: `final_answer`, `thinking`, `tool_calls`, `trace_id`, `tokens.input`, `tokens.output`, `tokens.thinking`. Provided by `provider` presets; override per field as needed. |
| `systems[].response_mapping.thinking` | JSONPath | no | Where reasoning/thinking text lives in the response. Captured into `Trace.output.thinking`, never folded into `final_answer`. See [DataModel.md](DataModel.md) and [Adapters.md](Adapters.md). |
| `systems[].response_mapping.tokens.thinking` | JSONPath | no | Where the thinking-token count lives in the response. Captured into `Trace.metrics.token_thinking`. |
| `systems[].request_template` | string | no | Jinja-like template rendered into the request body. Provided by `provider` presets. |
| `systems[].metadata` | dict | no | Tags surfaced in summary (e.g., branch, model, prompt_version). |
| `systems[].timeout_seconds` | int | no | Default 120. Per-cell timeout. |
| `systems[].stream` | bool | no | Enable streaming response handling. |
| `systems[].stream_format` | enum | conditional | `sse` \| `json_lines` \| `raw_chunks`. Required when `stream: true`. |
| `systems[].stream_event_field` | JSONPath | conditional | Extract a token from each stream event. |
| `systems[].stream_done_field` | JSONPath | no | Field that signals stream completion. |
| `systems[].response_mapping` | dict | no | JSONPath rules to populate Trace fields. |
| `systems[].enrich_trace_from` | list | no | TraceEnrichers run after the SystemAdapter. See [Observability.md](Observability.md). |
| `systems[].*` | adapter-specific | per adapter | See [Adapters.md](Adapters.md). |
| `workspace.type` | enum | conditional | Required if any evaluator inspects filesystem. See [Filesystem.md](Filesystem.md). |
| `workspace.copy_from` | string | conditional | Initial state for the system to modify. |
| `trace.capture` | list[string] | no | Hint to SystemAdapter; capturing more than requested is allowed. |
| `evaluators` | list | yes | At least one entry. |
| `evaluators[].name` | string | yes | Unique within run. Referenced by `pass_criteria`. |
| `evaluators[].type` | enum | yes | One of the registered evaluators. See [Evaluators.md](Evaluators.md). |
| `evaluators[].config` | dict | per type | Schema is enforced by the evaluator. |
| `pass_criteria.all_required` | list[string] | no | All listed evaluators must pass for the case to pass. |
| `pass_criteria.any_required` | list[string] | no | Any one passing satisfies this clause. |
| `run.max_concurrency` | int | no | Default 4. Cells running in parallel, system-wide. |
| `run.per_variant_concurrency` | int | no | Optional override per variant. |
| `run.retry.max_attempts` | int | no | Default 1 (no retries). |
| `run.retry.on` | list[enum] | no | Retry only on these error types: `timeout`, `http_5xx`, `adapter_error`. |
| `run.retry.backoff_seconds` | float | no | Exponential base. |
| `run.baseline_variant` | string | no | Used by ComparisonReport in `summary.yaml`. |
| `run.cost_limit_usd` | float | no | Run-level cost guardrail. When accumulated `trace.metrics.cost_usd` across completed cells reaches this value, queued cells are short-circuited with a `cost_limit` Trace. Independent from and additive to per-evaluator `cost_limit_usd`. |
| `output[]` | list[dict] | yes | At least one TraceStore. Single mapping is accepted and coerced to a one-element list. |
| `output[].type` | enum | yes | One of the registered TraceStores. |
| `output[].path` | string | type-dependent | Required for `local_files`. |

For adapter-specific fields (`endpoint`, `headers`, `query_params`, `command`, `image`, `branch`, etc.), see the matching adapter section in [Adapters.md](Adapters.md). Each adapter owns its sub-schema; the factory layer validates it.

### Env-var expansion

Strings of the form `${VAR}` are expanded from `os.environ` at load. Missing vars are a load error unless the value is annotated with `${VAR:-default}`.

### Validation order

1. Parse YAML.
2. Resolve env vars.
3. Run Pydantic validation against the top-level schema.
4. For each `systems[]`, `evaluators[]`, `dataset`, `workspace`, `output[]`: dispatch to the matching factory's schema (each factory owns its sub-schema).
5. Cross-reference checks: `pass_criteria.*` names exist in `evaluators[]`; `run.baseline_variant` exists in `systems[]`.

A bad config never reaches the runner.

---

## `cases.yaml`

Working sample: [`examples/listing_price/cases.yaml`](../examples/listing_price/cases.yaml).

### Top-level structure

```text
schema_version   "1.0" — bump triggers validation against newer schema
dataset          identity (name, description, owner)
cases            list of EvalCase
```

### Case fields

| Path | Type | Required | Notes |
|---|---|---|---|
| `cases[].id` | string | yes | Unique within the file. Becomes `case_id` in traces and results. |
| `cases[].input` | dict | yes | Opaque to the runner — the SystemAdapter parses it. |
| `cases[].metadata` | dict | no | Filterable from `eval.yaml > dataset.filter`. |
| `cases[].expected.must_call_tools` | list[string] | no | Used by `tool_called` evaluators. |
| `cases[].expected.answer_should_include` | list[string] | no | Used by `contains_text` evaluators. |
| `cases[].expected.answer_should_not_include` | list[string] | no | Same. |
| `cases[].expected.facts` | dict | no | Ground-truth values evaluators may compare against. |
| `cases[].expected.must_modify_files` | list[string] | no | For filesystem-modifying evals. |
| `cases[].expected.must_not_modify_files` | list[string] | no | Same. |

### Rules

- `id` is unique within the file.
- `input` is opaque to the runner. The system adapter knows what to do with it.
- `metadata` is filterable from `eval.yaml > dataset.filter`.
- `expected` is a hint. Evaluators may ignore it. Each evaluator declares which `expected` keys it consumes — see [Evaluators.md](Evaluators.md).
- The dataset file commits a `schema_version`. Bumping it triggers validation against a newer schema.

### Why YAML, not JSON

YAML is the user-facing format. Reasons:
- Multi-line strings (rubrics, prompts) without escaping.
- Comments — datasets accumulate context, and comments document why a case exists.
- Less brace noise; people edit it by hand.

JSON is fine for machine-to-machine artifacts (`traces.jsonl`, `results.jsonl`). YAML for everything authored by humans.

---

## Schema versioning

Every authored file declares `schema_version`. Eval Harness:

- Reads any `1.x` file.
- Refuses to read `2.x` until you upgrade.
- Writes the version it produced into `summary.yaml`.

We do not auto-migrate authored files. We do auto-migrate produced files (traces, results) if the on-disk version is older than the running version.
