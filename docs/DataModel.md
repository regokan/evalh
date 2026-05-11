# Data Model

Four nouns. One trace schema. One result schema. Everything else is derived.

```text
EvalCase     one dataset row
RunVariant   one configured way to invoke the system
Trace        what happened during one (case × variant)
EvaluationResult   judgment of one trace by one evaluator
RunSummary   aggregate of all results in a run
```

All types carry `schema_version`. v0 is `"1.0"`. We commit to never breaking `1.x` readers.

---

## EvalCase

A single test case. Lives in `cases.yaml`. The runner never modifies it.

```python
class EvalCase(BaseModel):
    schema_version: str = "1.0"
    id: str
    input: dict                      # opaque to the runner; the system adapter knows what to do with it
    metadata: dict = {}              # arbitrary; useful for filtering and reporting
    expected: ExpectedBehavior = ExpectedBehavior()

class ExpectedBehavior(BaseModel):
    must_call_tools: list[str] = []
    answer_should_include: list[str] = []
    answer_should_not_include: list[str] = []
    facts: dict = {}                 # ground-truth values evaluators may compare against
    must_modify_files: list[str] = []  # for filesystem-modifying evals
    must_not_modify_files: list[str] = []
```

YAML form:

```yaml
cases:
  - id: listing_price_001
    input:
      user_message: "What is the average house price near listing ABC123?"
    metadata:
      listing_id: ABC123
      suburb: Richmond
    expected:
      must_call_tools: [get_listing_details, get_average_suburb_price]
      answer_should_include: [Richmond, average]
      facts:
        suburb_average_price: 1200000
```

`input` is whatever your system adapter expects. The runner does not parse it.

---

## RunVariant

One row of the variant matrix. Built from `eval.yaml > systems[]`.

```python
class RunVariant(BaseModel):
    schema_version: str = "1.0"
    name: str                        # e.g. "agent_main", "agent_experimental"
    adapter: str                     # e.g. "http", "python_function", "git_branch"
    config: dict                     # adapter-specific config
    metadata: dict = {}              # tags: model, prompt_version, branch, etc.
```

Variants exist so the runner does not need to distinguish "system" from "experiment." Both are the same thing: a row in the matrix. See [Variants.md](Variants.md).

---

## Trace

The single most important object in the system. Everything else aggregates traces.

```python
class Trace(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    case_id: str
    variant_name: str
    started_at: datetime
    finished_at: datetime
    latency_ms: int

    input: dict
    output: TraceOutput

    messages: list[TraceMessage] = []
    tool_calls: list[ToolCall] = []
    tool_results: list[ToolResult] = []

    metrics: TraceMetrics = TraceMetrics()
    error: TraceError | None = None
    extra: dict = {}                 # adapter-specific overflow
    # Common keys placed in `extra` by adapters:
    #   trace_id           upstream platform trace id (Langfuse, Phoenix, etc.) — used by TraceEnrichers
    #   enrichment_errors  list of {enricher, error} when a TraceEnricher failed non-fatally
    #   span_id            OTel span id, when emitted via OTel TraceStore

class TraceOutput(BaseModel):
    final_answer: str | None = None
    thinking: str | None = None       # consolidated thinking/reasoning text, if the system surfaces it
    structured: dict | None = None

class TraceMessage(BaseModel):
    role: str                        # "user" | "assistant" | "tool" | "system"
    content: str | dict | None = None
    thinking: str | None = None      # for assistant turns that produced thinking blocks
    tool_call: ToolCall | None = None
    name: str | None = None          # tool name when role == "tool"

class ToolCall(BaseModel):
    id: str | None = None
    name: str
    arguments: dict
    started_at: datetime | None = None

class ToolResult(BaseModel):
    tool_call_id: str | None = None
    name: str
    content: dict | str

class TraceMetrics(BaseModel):
    token_input: int | None = None
    token_output: int | None = None
    token_thinking: int | None = None      # reasoning / thinking tokens, billed separately by most providers
    cost_usd: float | None = None          # total billed cost (input + output + thinking)
    cost_thinking_usd: float | None = None # thinking-token portion, when the provider distinguishes
    # Streaming-specific (set by streaming SystemAdapters; None for non-streaming)
    latency_first_token_ms: int | None = None
    latency_last_token_ms: int | None = None
    tokens_per_second: float | None = None
    stream_chunks: int | None = None
    stream_completed: bool | None = None
    # Free-form overflow: retries, queue_wait_ms, judge_cost_usd, etc.
    custom: dict = {}

class TraceError(BaseModel):
    type: str                        # "timeout" | "http_5xx" | "adapter_error" | "exception"
    message: str
    stack: str | None = None
```

YAML form (one trace, persisted as a line in `traces.jsonl`):

```yaml
schema_version: "1.0"
run_id: 2026-05-03T10-30-00_listing_price_eval
case_id: listing_price_001
variant_name: agent_experimental
started_at: 2026-05-03T10:30:14.221Z
finished_at: 2026-05-03T10:30:17.061Z
latency_ms: 2840
input:
  user_message: "What is the average house price near listing ABC123?"
output:
  final_answer: "The listing is in Richmond. The average house price is $1.2M..."
messages:
  - { role: user, content: "What is the average house price near listing ABC123?" }
  - role: assistant
    tool_call: { name: get_listing_details, arguments: { listing_id: ABC123 } }
  - role: tool
    name: get_listing_details
    content: { suburb: Richmond, price: 1350000 }
tool_calls:
  - { name: get_listing_details, arguments: { listing_id: ABC123 } }
  - { name: get_average_suburb_price, arguments: { suburb: Richmond } }
metrics:
  token_input: 1520
  token_output: 210
  cost_usd: 0.012
error: null
```

### Trace invariants
- `started_at` and `finished_at` are always set, even on error.
- `latency_ms == (finished_at - started_at).total_milliseconds()`. The runner enforces this — adapters cannot lie about latency.
- `error` is set if and only if the adapter raised. `output.final_answer` may still be set if the adapter captured a partial response.
- `tool_calls` and `tool_results` are derived views of `messages`. They are stored explicitly so evaluators do not have to walk `messages`.

### Why traces are write-once — and what that constrains

The runner persists traces to `traces.jsonl` *before* running evaluators. Three consequences follow:

- **If an evaluator crashes, the trace survives.** Evaluator failures isolate to one cell × one evaluator; the trace remains for the others.
- **`evalh re-evaluate <run_id>` reads from disk.** It never re-calls the system. This is what makes adding a new evaluator to an old run cheap and deterministic (modulo `llm_judge` stochasticity).
- **Evaluators must be pure functions of `(case, trace, artifact)`.** Don't put state in them. Don't read environment vars in them. Don't pass info adapter → evaluator out-of-band — if the data matters, **add it to the trace schema**.

If a trace can't answer a question that needs answering, the right move is to *extend the trace schema* (additive only — see "Schema versioning" below), not to bolt a side channel onto an evaluator.

### Schema versioning

Every persisted type carries `schema_version`. v0 is `"1.0"`. We commit to never breaking `1.x` readers — only additive changes within a major.

#### Bumping `schema_version` — checklist

When you bump the major version on any of `Trace`, `EvalCase`, `EvaluationResult`, `RunSummary`, `FilesystemArtifact` (or rename/remove a config key, or change a field's semantics — those are also breaking):

- [ ] Old `1.x` files still load (additive only) OR migration code added.
- [ ] `BREAKING CHANGE:` footer in the commit body.
- [ ] Migration section in the PR description.
- [ ] Bump pre-1.0 minor version (`0.0.x` → `0.1.0`).

If you can't tick all four, don't bump the schema.

We auto-migrate produced files (`traces.jsonl`, `results.jsonl`) on read when the on-disk version is older than the running version. We do **not** auto-migrate authored files (`eval.yaml`, `cases.yaml`) — bumping their schema is an explicit user action.

### Thinking / reasoning content

Thinking is captured as a first-class field — never folded into `final_answer`.

| Field | What it holds |
|---|---|
| `output.thinking` | Consolidated thinking text for the response (when the provider surfaces it) |
| `messages[].thinking` | Per-turn thinking, when an assistant turn produced a thinking block |
| `metrics.token_thinking` | Tokens spent on thinking. Tracked separately because providers bill it separately. |
| `metrics.cost_thinking_usd` | Thinking-only cost portion, when the provider distinguishes |

**Rules:**
- `final_answer` is what the user-facing answer is. `thinking` is what the model reasoned. They are never concatenated.
- If a provider hides the thinking text (e.g., OpenAI o-series often returns only a token count), set `output.thinking = None` and `metrics.token_thinking = <count>`. We record what we know.
- `metrics.cost_usd` is the *total* billed cost. `cost_thinking_usd` is a sub-field, not additive.
- Evaluators choose explicitly whether to consider thinking. By default, `contains_text` and `llm_judge` operate on `final_answer`. Pass `field: output.thinking` to evaluate the reasoning instead. See [Evaluators.md](Evaluators.md).

**Provider mapping cheat-sheet** for the HTTP adapter's `response_mapping`:

| Provider | Thinking text | Thinking-token count |
|---|---|---|
| Claude (extended thinking) | `$.content[?(@.type=="thinking")].thinking` | `$.usage.thinking_tokens` (or per the SDK's reported field) |
| OpenAI o-series | not returned | `$.usage.completion_tokens_details.reasoning_tokens` |
| DeepSeek-R1 | inline `<think>...</think>` blocks; the adapter strips them into `output.thinking` | not separately reported |
| Gemini 2.5 thinking | `$.candidates[0].content.parts[?(@.thought==true)].text` | `$.usage_metadata.thoughts_token_count` |
| Your agent service | whatever your service forwards | whatever your service emits |

---

## EvaluationResult

One judgment by one evaluator on one trace.

```python
class EvaluationResult(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    case_id: str
    variant_name: str
    evaluator: str                   # name from eval.yaml > evaluators[].name
    evaluator_type: str              # type from eval.yaml > evaluators[].type
    passed: bool
    score: float | None = None       # numeric where applicable, e.g. 0–5 for llm_judge
    reason: str
    detail: dict = {}                # evaluator-specific data
    started_at: datetime
    finished_at: datetime
    latency_ms: int
    error: TraceError | None = None
```

YAML form:

```yaml
- evaluator: must_call_listing_tool
  evaluator_type: tool_called
  passed: true
  score: 1.0
  reason: "Tool get_listing_details was called."
- evaluator: answer_quality
  evaluator_type: llm_judge
  passed: true
  score: 4
  reason: "Answer correctly compares listing price against suburb average."
  detail:
    judge_model: claude-4-7
    judge_prompt_hash: 8e2f...
```

### Why every evaluator returns the same shape
A deterministic check (`tool_called`) and an LLM judge (`llm_judge`) produce the same `EvaluationResult` so the summary code does not branch on type. `passed` is the canonical pass/fail; `score` is optional for ranking; `reason` is for humans; `detail` is for debugging.

---

## RunSummary

Aggregate of one run.

```python
class RunSummary(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    started_at: datetime
    finished_at: datetime
    config_path: str
    config_hash: str

    cases_total: int
    variants: list[VariantSummary]
    by_evaluator: list[EvaluatorRollup]
    comparison: ComparisonReport | None = None

class VariantSummary(BaseModel):
    name: str
    cases_total: int
    cases_passed: int
    cases_errored: int
    pass_rate: float
    avg_latency_ms: float
    avg_cost_usd: float | None
    avg_tokens_input: float | None
    avg_tokens_output: float | None

class EvaluatorRollup(BaseModel):
    evaluator: str
    by_variant: dict[str, EvaluatorVariantRollup]

class EvaluatorVariantRollup(BaseModel):
    pass_rate: float
    avg_score: float | None

class ComparisonReport(BaseModel):
    baseline: str                    # variant name
    deltas: list[VariantDelta]

class VariantDelta(BaseModel):
    variant: str
    pass_rate_delta: float
    avg_latency_delta_ms: float
    regressions: list[str]           # case_ids that pass on baseline, fail on this variant
    improvements: list[str]
```

The summary is what humans read. It is regenerable from `traces.jsonl` + `results.jsonl`, so it is safe to delete and recompute.

---

## FilesystemArtifact (filesystem evals only)

```python
class FilesystemArtifact(BaseModel):
    schema_version: str = "1.0"
    case_id: str
    variant_name: str
    workspace_kind: str              # "tempdir_snapshot" | "git_branch" | "docker"
    before_manifest: FileManifest
    after_manifest: FileManifest
    diff: FileDiff
    artifacts_path: str              # path on disk where files live (relative to run dir)

class FileManifest(BaseModel):
    files: dict[str, FileEntry]      # path -> entry

class FileEntry(BaseModel):
    size: int
    mode: int
    mtime: float
    sha256: str

class FileDiff(BaseModel):
    added: list[str]
    removed: list[str]
    modified: list[str]              # paths whose sha256 changed
    text_diffs: dict[str, str] = {}  # optional unified-diff bodies for text files
```

This is the no-git path. See [Filesystem.md](Filesystem.md).

---

## On-disk layout for one run

```text
runs/2026-05-03T10-30-00_listing_price_eval/
  config.yaml                  # exact config used (including resolved env vars masked)
  config_hash.txt
  traces.jsonl                 # one Trace per line
  results.jsonl                # one EvaluationResult per line
  summary.yaml                 # RunSummary
  artifacts/                   # filesystem artifacts, if any
    listing_price_001/
      agent_experimental/
        before/
        after/
        diff.txt
```

This is the durable surface. Anything not in this folder is regenerable or unimportant.
