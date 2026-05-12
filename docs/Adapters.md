# Adapters & Factories

This is the most-asked design question, so it gets its own document:

> **What is the difference between an adapter and a factory?**

Short answer:

| | Adapter | Factory |
|---|---|---|
| **What it is** | A live object that talks to the outside world | A function that builds the right adapter from a config dict |
| **When it runs** | At run time (every case) | At plan time (once per run) |
| **Knows about** | One specific external system (HTTP, git, Postgres) | All registered adapter types of one kind |
| **Returns** | Traces, results, manifests | An adapter instance |
| **Analogy** | A driver behind the wheel | The dispatcher that hands you keys |

You can have many factories (one per adapter family) and many adapters (one per registered type). The runner only ever holds adapters; it never sees factory code.

```mermaid
flowchart LR
    YAML[eval.yaml<br/>systems[].adapter: http] -- "1. read type" --> F[SystemAdapterFactory]
    F -- "2. lookup in registry" --> REG[Registry<br/>http → HttpSystemAdapter]
    F -- "3. validate config" --> V[adapter sub-schema]
    F -- "4. instantiate" --> A[HttpSystemAdapter instance]
    A -- "5. handed to runner" --> RUNNER[Runner]
    RUNNER -- "6. run(case, variant)" --> A
```

Every "string in YAML → behavior" arrow goes through a factory. Every "behavior → outside world" arrow goes through an adapter.

---

## The six adapter families

```text
DatasetAdapter      load EvalCase[] from somewhere
SystemAdapter       call the system under test, return Trace
TraceEnricher       fetch external context (e.g. upstream platform trace) and merge into Trace
TraceStore          persist Trace / EvaluationResult / RunSummary
WorkspaceAdapter    prepare an isolated filesystem, collect a diff
EvaluatorFactory    build evaluators from config (technically a factory, listed for symmetry)
```

Each family has a base Protocol in `eval_harness/core/` and a registry in `eval_harness/factories/`.

---

## DatasetAdapter

Where cases come from. v0 ships one implementation: `yaml`.

```python
class DatasetAdapter(Protocol):
    async def load_cases(self) -> list[EvalCase]: ...
```

| Type | Config | Notes |
|---|---|---|
| `yaml` (v0) | `path` | Loads from a YAML file. |
| `jsonl` (v0.1) | `path` | One case per line. |
| `postgres` (v1) | `dsn`, `query` | Maps rows to EvalCase via a mapping config. |
| `langfuse` (v1) | `host`, `project_id`, `filter`, `embed_full_trace?` | Pulls historical traces. |
| `phoenix` (v1) | `endpoint`, `filter`, `embed_full_trace?` | Pulls from Arize Phoenix. |
| `arize` (v1.x) | `space_id`, `model_id`, `filter`, `embed_full_trace?` | Pulls from hosted Arize. |
| `sheet` (future) | `spreadsheet_id`, `sheet_name`, `column_map` | Google Sheets. |

Filtering on dataset metadata happens *after* loading; the planner applies `dataset.filter`. Platform-side filters (`tags`, time range, score band) belong on the adapter config so they push down to the platform's API and avoid pulling everything.

### Two modes for observability-platform DatasetAdapters

Adapters that pull from observability platforms support two modes, selected by `embed_full_trace`:

| Mode | `embed_full_trace` | What the case carries | Use with |
|---|---|---|---|
| **Inputs only** (default) | `false` | `case.input` only — the user's request as captured by the platform | Fresh SystemAdapter (HTTP / fn / CLI). Use for backtesting: "rerun this input on a candidate." |
| **Full trace** | `true` | `case.input` + the full embedded `Trace` (final answer, tool calls, metrics, ...) on `case._embedded_trace` | `replay` SystemAdapter. Use for online evaluation: "score what already happened." |

Both modes can coexist in one run — the same dataset feeds both `replay` (no system call) and an `http` candidate (fresh call), and the runner expands them as parallel variants. See [Observability.md → Pattern 4](Observability.md#pattern-4-online-evaluation).

```yaml
dataset:
  type: langfuse
  api_key: ${LANGFUSE_API_KEY}
  host: ${LANGFUSE_HOST}
  filter:
    tags: [production]
    timestamp_gt: "2026-04-26T00:00:00Z"
    user_score_lt: 0.5
  sample: 200
  embed_full_trace: true              # required if any variant uses adapter: replay
```

---

## SystemAdapter

The most-extended family. Each one knows how to invoke one kind of system.

```python
class SystemAdapter(Protocol):
    name: str

    async def open(self) -> None:
        """Called once per run before any case dispatches.
        Use for healthchecks, warmup, opening connection pools."""

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        """Invoke the system. Always return a Trace, even on error."""

    async def close(self) -> None:
        """Called once per run after all cases complete."""
```

### v0: `http`

The HTTP adapter is the most-used adapter. It calls **the system under test** — typically your agent service — and returns whatever the agent's HTTP API returned, mapped into a `Trace`.

> **Two boundaries to keep straight.**
>
> - **Boundary 1: Eval Harness ↔ system under test.** This is where the HTTP adapter sits. We call your agent. Your agent's HTTP response shape is whatever you wrote it to be.
> - **Boundary 2: System ↔ LLM provider.** This is where Langfuse / LangSmith / Arize / Phoenix sit. They wrap the *internal* LLM calls your system makes. **Eval Harness does not touch this boundary.** We don't see those calls; we see the agent's API response.
>
> So `response_mapping` does *not* mean "tell us how OpenAI shapes its responses." It means "in the JSON your agent returns, here's where the final answer lives." Usually one or two lines. See [Observability.md](Observability.md) for how the two layers compose.

#### The common case: evaluating your agent service

Your agent's HTTP response is something like `{"answer": "...", "tool_calls": [...]}`. The mapping is trivial:

```yaml
systems:
  - name: agent_main
    adapter: http
    endpoint: http://localhost:8000/chat
    timeout_seconds: 60
    headers:
      Authorization: "Bearer ${AGENT_API_KEY}"
    query_params:
      variant: main
    request_template: |                       # optional; defaults to passing case.input as JSON body
      { "user_message": "{{ input.user_message }}", "session_id": "{{ case_id }}" }
    response_mapping:                         # where to find each trace field in your agent's response
      final_answer: $.answer
      tool_calls: $.tool_calls
      trace_id: $.langfuse_trace_id           # optional, for TraceEnricher (see Observability.md)
```

That is the entire surface for 95% of agent evals. No knowledge of OpenAI, Anthropic, or any LLM provider — your agent already did that work.

#### The niche case: evaluating an LLM endpoint directly

Sometimes the system under test *is* an LLM endpoint — pure prompt evals or model-vs-model comparisons with no agent in between. For that case, `provider:` presets save you from writing the JSONPath every time.

```yaml
# Direct LLM evaluation (no agent in front of the model)
systems:
  - name: prompt_v1
    adapter: http
    provider: anthropic_messages             # preset auto-fills request_template + response_mapping
    endpoint: https://api.anthropic.com/v1/messages
    headers:
      x-api-key: "${ANTHROPIC_API_KEY}"
      anthropic-version: "2023-06-01"
    body:
      model: claude-4-7
      max_tokens: 1024
```

Shipped presets:

| `provider:` value | For |
|---|---|
| `openai_chat` | OpenAI `/v1/chat/completions` |
| `anthropic_messages` | Anthropic `/v1/messages` |
| `langserve_invoke` | LangServe `/invoke` |
| `simple` | `{"input": ...} → {"output": ...}` toy shape |

Each preset is just a saved `request_template` + `response_mapping` in `eval_harness/adapters/system/presets/`. They exist to spare you from typing JSONPath for well-known APIs — they are not load-bearing. Override any field if your shape diverges.

#### Why `response_mapping` exists at all

Different responses live at different places. We could:
1. Hardcode a default shape — breaks for anyone whose agent returns something else.
2. Ship one adapter per response shape — explodes the adapter count.
3. Take JSONPath at config time — one adapter, every shape.

We do (3). It's the smallest contract that handles every system without growing per-system code. Provider presets are option (1) on top of (3) for direct-LLM evals where the JSONPath is well-known.

#### Capturing thinking / reasoning content

Modern models emit thinking separately from the final answer. The HTTP adapter captures it as a first-class trace field, never folded into `final_answer`. See [DataModel.md → Thinking](DataModel.md#thinking--reasoning-content) for the data-model rules.

`response_mapping` accepts `thinking:` and `tokens.thinking:`:

```yaml
# Claude — extended thinking
systems:
  - name: claude_thinker
    adapter: http
    provider: anthropic_messages          # preset already maps thinking; shown explicitly here for clarity
    endpoint: https://api.anthropic.com/v1/messages
    headers:
      x-api-key: "${ANTHROPIC_API_KEY}"
      anthropic-version: "2023-06-01"
    body:
      model: claude-4-7
      thinking: { type: enabled, budget_tokens: 4000 }
    response_mapping:
      final_answer: $.content[?(@.type=="text")].text
      thinking: $.content[?(@.type=="thinking")].thinking
      tokens.input: $.usage.input_tokens
      tokens.output: $.usage.output_tokens
      tokens.thinking: $.usage.thinking_tokens
```

```yaml
# OpenAI o-series — reasoning tokens are returned as a count, not text
systems:
  - name: openai_o3
    adapter: http
    provider: openai_chat
    endpoint: https://api.openai.com/v1/chat/completions
    headers:
      Authorization: "Bearer ${OPENAI_API_KEY}"
    body:
      model: gpt-5.5
      reasoning_effort: high
    response_mapping:
      final_answer: $.choices[0].message.content
      tokens.input: $.usage.prompt_tokens
      tokens.output: $.usage.completion_tokens
      tokens.thinking: $.usage.completion_tokens_details.reasoning_tokens
      # `thinking:` intentionally omitted — the API doesn't return reasoning text
```

```yaml
# DeepSeek-R1 — inline <think>...</think> blocks
systems:
  - name: deepseek_r1
    adapter: http
    provider: deepseek_r1                 # preset; strips <think> blocks into trace.output.thinking
    endpoint: https://api.deepseek.com/v1/chat/completions
    headers:
      Authorization: "Bearer ${DEEPSEEK_API_KEY}"
    body:
      model: deepseek-r1
```

```yaml
# Your agent service — passes thinking through verbatim
systems:
  - name: agent_main
    adapter: http
    endpoint: http://localhost:8000/chat
    response_mapping:
      final_answer: $.answer
      thinking: $.thinking
      tool_calls: $.tool_calls
      tokens.thinking: $.usage.thinking_tokens
```

**Rules the adapter enforces:**
- If the provider response includes thinking content, capture it. Never silently drop.
- Thinking text never enters `final_answer` even if the system mistakenly emits both inline. Adapters parse it out (e.g., DeepSeek's `<think>` blocks).
- If a provider only reports a thinking-token count without text, set `output.thinking = None` and record the count. Don't fabricate placeholder text.
- Latency for streaming systems counts the thinking phase: `latency_first_token_ms` is time-to-first-output-token, not time-to-first-thinking-token. Time-to-first-thinking-token (when meaningful) goes in `metrics.custom.latency_first_thinking_token_ms`.

#### Streaming systems

There are two senses of "streaming" — different answers for each.

**(a) The system streams tokens** (SSE / chunked HTTP / websocket). The model emits tokens incrementally rather than as one final blob.

> Eval Harness handles this in the SystemAdapter. The runner doesn't care. The adapter is responsible for returning one complete `Trace` per cell — *how it assembles that trace internally* is its business.

**(b) The system streams traces to an observability platform during execution.** The system itself pushes events to Langfuse / Phoenix / Arize as it runs.

> Not Eval Harness's job during the run. The platform handles live ingestion. We consume the finished trace at end-of-cell. If you want our results to appear in the platform progressively, configure the platform as a `TraceStore` sink — `save_trace` is called per cell, so traces land as cells complete. See [Observability.md](Observability.md).

The rest of this section is about (a). For (a), opt in:

```yaml
systems:
  - name: agent_streaming
    adapter: http
    endpoint: http://localhost:8000/chat
    stream: true
    stream_format: sse                          # sse | json_lines | raw_chunks
    stream_event_field: $.choices[0].delta.content    # extract token from each event
    stream_done_field: $.choices[0].finish_reason     # signal of stream end
    stream_tool_call_field: $.choices[0].delta.tool_calls   # optional, for streamed tool calls
```

The adapter accumulates tokens, captures stream-specific metrics into `TraceMetrics.custom`, and still returns one complete `Trace` per cell. The runner is unchanged.

Captured stream metrics:

| Field | Meaning |
|---|---|
| `latency_first_token_ms` | TTFT — most-asked latency stat for streaming |
| `latency_last_token_ms` | total stream wall time |
| `tokens_per_second` | output throughput |
| `stream_chunks` | chunk count |
| `stream_completed` | true iff `finish_reason` was set |

These power evaluators like `latency_first_token_under` and `tokens_per_second_above` (see [Evaluators.md](Evaluators.md)).

What the harness does **not** do:
- Push partial traces. A `Trace` is written once, when the cell completes.
- Stream tokens to a UI. That's what observability platforms are for.
- Resume an interrupted stream. Failed cells emit a Trace with `error` and `stream_completed: false`.

### v0: `python_function`

```yaml
systems:
  - name: agent_local
    adapter: python_function
    target: my_agent.run                    # importable callable
    init_kwargs:
      model: claude-4-7
      tools_module: my_agent.tools
```

Calls `my_agent.run(case, variant)` directly. No process boundary. Useful for unit-test-shaped evals and local iteration.

### v0.1: `cli`

```yaml
systems:
  - name: claude_code
    adapter: cli
    command: ["claude", "--print", "--input-format", "json"]
    cwd: ${WORKSPACE_PATH}
    env:
      ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
    stdin_template: '{ "prompt": "{{ input.user_message }}" }'
    parse_stdout_as: json
```

Spawns a subprocess per case. Useful for evaluating CLIs (Claude Code, Cursor, Aider).

### v1: `git_branch`

```yaml
systems:
  - name: eval_refactor
    adapter: git_branch
    repo_path: ../my-agent
    branch: feat/eval-refactor
    start_command: ["uvicorn", "app:app", "--port", "0"]   # 0 = pick a port
    healthcheck: GET /health
    inner_adapter: http                     # what to do once it's up
    inner_config:
      endpoint: "http://localhost:{port}/chat"
```

Checks out a branch into a tempdir, starts the service, runs cases, tears down. Composes with `inner_adapter` so it does not duplicate HTTP or CLI logic.

### v1: `docker`

```yaml
systems:
  - name: agent_v3_docker
    adapter: docker
    image: registry/agent:v3
    inner_adapter: http
    inner_config:
      endpoint: "http://localhost:{port}/chat"
    healthcheck: GET /health
```

Same idea, but the system is a Docker image instead of a branch.

### v1: `replay` — for online evaluation

The `replay` SystemAdapter does not invoke any system. It returns a `Trace` that the DatasetAdapter already fetched and embedded in the case. Used for **online evaluation** (a.k.a. trace replay): pull historical traces from an observability platform, run evaluators against them, store results.

```yaml
systems:
  - name: production_replay
    adapter: replay
    metadata:
      source: langfuse-production
```

There is almost no config — the `replay` adapter just unwraps `case._embedded_trace` (set by a DatasetAdapter that opted into `embed_full_trace: true`) and returns it.

What the adapter sets on the returned trace:

| Field | Value |
|---|---|
| `Trace.extra.source` | `"replay"` — distinguishes replayed traces from fresh ones in `traces.jsonl` |
| `Trace.extra.replayed_from` | `{ platform, trace_id, fetched_at }` — provenance |
| `Trace.started_at` / `finished_at` | The **original** timestamps (the trace describes what happened, not what the harness did with it) |
| `Trace.metrics.*` | Original numbers — never lie about latency by recording the replay's near-zero wall time |
| `Trace.run_id` / `case_id` / `variant_name` | Set to current run, current case, current variant — so evaluators and the trace store can join |

Why this earns its own SystemAdapter (rather than being a runner mode flag): it composes with normal variants. A single run can:

1. `replay` the production trace as it actually ran, AND
2. `http`-call a candidate system version with the same input, AND
3. `python_function`-call a local prototype with the same input,

…and feed all three to the same evaluators. That's free backtesting against historical traffic.

See [Observability.md → Pattern 4](Observability.md#pattern-4-online-evaluation) and [`examples/online_eval/`](../examples/online_eval/) for full configuration.

### v1: `user_simulator` — multi-turn conversations

`user_simulator` drives a multi-turn conversation by playing the user role with an LLM. It is a SystemAdapter (no new family) and composes with an `inner_system` adapter that handles the actual system-under-test call each turn. The user-role LLM is resolved through the shared `LlmBackend` registry (same one `llm_judge` uses), so adding a backend lights up both consumers.

```yaml
systems:
  - name: agent_conversational
    adapter: user_simulator
    user_model: claude-4-7
    user_persona_prompt: |
      You are a curious real-estate buyer asking about listings. Keep
      questions short and concrete. Stop when satisfied.
    max_turns: 6
    stopping_criterion:
      type: content_match
      patterns: ["thanks", "that's all"]
      case_sensitive: false
    inner_system:                       # configured like any SystemAdapter
      adapter: http
      endpoint: http://localhost:8000/chat
      response_mapping:
        final_answer: $.answer
        tool_calls: $.tool_calls
    cost_limit_usd: 0.20                # per-call cap on the user-role LLM
```

Three stopping criteria, all gated by `max_turns` as a hard ceiling:

| `type` | Extra fields | Stops when |
|---|---|---|
| `turn_count` | `n` | after `n` assistant replies |
| `content_match` | `patterns: list[str]`, `case_sensitive?: bool` | latest assistant turn contains any pattern |
| `judge` | `model`, `question` | a judge LLM answers `yes` to `question` |

Per turn the wrapper:
1. Appends the current user message to the running conversation.
2. Synthesises a per-turn case (`case.input.user_message` + `case.input.conversation`) and calls `inner_system.run(...)`. Inner adapters that only know about `user_message` keep working; multi-turn-aware inners can read `conversation`.
3. Appends the assistant reply, accumulates metrics, tool calls, and tool results.
4. Checks the stopping criterion; if not satisfied, calls the user-role backend to produce the next user message.

The final `Trace`:
- `output.final_answer` = last assistant turn.
- `output.thinking` = per-turn thinking concatenated (or `None` if no turn surfaced thinking).
- `messages` = the full alternating User / Assistant transcript.
- `tool_calls` / `tool_results` = aggregated across turns.
- `metrics` = summed tokens / cost across turns.
- `extra.user_simulator = { turns, stop_reason, user_model, max_turns }` for evaluators that want to gate on conversation length or the stop reason.

---

## TraceEnricher

Many production systems already emit rich traces to observability platforms (Langfuse, Phoenix, Arize). The system's HTTP response is intentionally thin — final answer + a `trace_id`. We don't want to re-implement that platform's tracing inside our HTTP adapter.

A `TraceEnricher` runs **after** the SystemAdapter returns. It takes the basic `Trace` plus a key (typically `trace_id` extracted from the response), fetches the upstream rich trace from the platform, and merges fields into our `Trace`.

```python
class TraceEnricher(Protocol):
    async def open(self) -> None: ...
    async def enrich(self, trace: Trace) -> Trace: ...
    async def close(self) -> None: ...
```

Configured per system, after `response_mapping`:

```yaml
systems:
  - name: agent_main
    adapter: http
    endpoint: http://localhost:8000/chat
    response_mapping:
      final_answer: $.answer
      trace_id: $.langfuse_trace_id           # extracted into Trace.extra.trace_id
    enrich_trace_from:
      - type: langfuse
        api_key: ${LANGFUSE_API_KEY}
        host: ${LANGFUSE_HOST}
        wait_for_ingestion_seconds: 2          # platform ingestion lag
        merge:
          messages: $.observations[*].input
          tool_calls: $.observations[?(@.type=="tool")]
          metrics.token_input: $.usage.input
          metrics.token_output: $.usage.output
          metrics.cost_usd: $.totalCost
```

| Type | Notes | Lands |
|---|---|---|
| `langfuse` | Fetch by `trace_id`. Honors ingestion lag with bounded polling. | v1 |
| `phoenix` | Fetch from Arize Phoenix by `span_id` / `trace_id`. | v1 |
| `arize` | Fetch from hosted Arize. | v1 |
| `otel` | Read from any OTel collector via `trace_id` (TraceQL-compatible backends). | v1 |
| `helicone` | Fetch by Helicone request-id. | v1.x |

Multiple enrichers can be chained — first Langfuse, then OTel — in declaration order. Each receives the `Trace` from the previous step.

Failure mode: an enricher that times out or errors **does not fail the cell**. It logs a warning into `Trace.extra.enrichment_errors` and the cell proceeds with the un-enriched trace. We never lose a result because an external platform was slow.

---

## TraceStore

Persistence. v0 ships `local_files`.

```python
class TraceStore(Protocol):
    async def open(self, run_id: str, run_dir: Path) -> None: ...
    async def save_trace(self, trace: Trace) -> None: ...
    async def save_evaluation(self, case_id: str, variant: str, results: list[EvaluationResult]) -> None: ...
    async def save_artifact(self, artifact: FilesystemArtifact) -> None: ...
    async def save_summary(self, summary: RunSummary) -> None: ...
    async def close(self) -> None: ...
```

| Type | Notes |
|---|---|
| `local_files` (v0) | Writes `traces.jsonl`, `results.jsonl`, `summary.yaml`. Append-only; safe to read mid-run. |
| `sqlite` (v0.1) | Single file. Useful when you want to query results from a notebook. |
| `postgres` (v1) | Multi-tenant; team-scale. |
| `langfuse` (v1) | Pushes traces to Langfuse so they appear in their UI. |
| `arize` (v1) | Same idea for Arize/Phoenix. |

Stores are independently swappable. You can run with `local_files` for development and `postgres` in CI without changing anything else.

### Trace store idempotency (v2)

The v2 dispatch primitive ([Executors.md](Executors.md)) uses deterministic `cell_id`s so retried cells can be detected at the sink. The Protocol gained an additive method:

```python
async def save_trace_idempotent(self, trace: Trace, cell_id: str) -> bool:
    """Returns True if written, False if `cell_id` already had a successful record.
    A previous error-state record is overwritten on retry."""
```

Three canonical sinks enforce the contract:

| Sink | Mechanism |
|---|---|
| `local_files` | sidecar marker `runs/<id>/cells/<cell_id>.success.marker` written after a successful save; subsequent calls check the marker first |
| `sqlite` | `traces.cell_id TEXT` column + `error_type` guard before UPSERT |
| `postgres` | indexed `eval_traces.cell_id TEXT` + `ON CONFLICT … WHERE existing.error_type IS NOT NULL` |

Other stores (otel, langfuse, phoenix, arize, braintrust, webhook) inherit the always-write fallback — the idempotency contract lives at the canonical sink (the first entry in `eval.yaml > output:`). Operators who want deduplication in their observability backend configure it there.

---

## WorkspaceAdapter

Optional. Required only when an evaluator inspects the filesystem.

```python
class WorkspaceAdapter(Protocol):
    async def prepare(self, case: EvalCase, variant: RunVariant) -> Workspace: ...
    async def collect_artifacts(self, workspace: Workspace) -> FilesystemArtifact: ...
    async def cleanup(self, workspace: Workspace) -> None: ...

class Workspace(BaseModel):
    path: Path
    metadata: dict
```

| Type | Notes |
|---|---|
| `tempdir_snapshot` (v0) | Copies `copy_from` to a tempdir, snapshots the manifest, returns the path. After the run, snapshots again and diffs. **No git required.** |
| `git` (v0.1) | Same as above but uses `git diff` for richer output and includes commit metadata in the manifest. |
| `docker_volume` (v1) | Workspace is a Docker volume; the system runs inside the container. |

The `tempdir_snapshot` implementation is the no-git path the user asked for. See [Filesystem.md](Filesystem.md) for the full algorithm.

---

## Evaluators are not adapters

Evaluators are *similar* to adapters (pluggable, registered, factory-built) but they have a different job: they read traces and emit results. They never touch the outside world. See [Evaluators.md](Evaluators.md).

The factory layer treats them the same way:

```python
class EvaluatorFactory:
    def build(self, config: EvaluatorConfig) -> Evaluator:
        cls = self.registry.get(config.type)
        cls.validate_config(config.config)
        return cls(name=config.name, **config.config)
```

---

## Registries

Each factory consults a registry. Registries are populated three ways:

1. **Built-in**, at import time: `eval_harness/adapters/system/__init__.py` registers `http` and `python_function`.
2. **Entry-points**, for third-party extensions:

   ```toml
   [project.entry-points."eval_harness.system_adapters"]
   my_custom_adapter = "my_pkg.adapters:MyAdapter"
   ```

3. **Programmatic**, for tests:

   ```python
   from eval_harness.factories import system_adapter_factory
   system_adapter_factory.register("fake", FakeSystemAdapter)
   ```

Once registered, the adapter is usable from `eval.yaml` immediately. The runner did not have to change.

---

## Why this split exists

A single `if/else` ladder in the runner would also work — for two adapter types. By the time you have five (HTTP, fn, CLI, git_branch, docker), the runner becomes a switch statement that nobody can review. The factory layer absorbs that complexity:

```text
runner       coordinates
factories    map config → instances
registries   map type names → classes
adapters     do the actual work
```

Each layer has one job. None of them know about the others' jobs. That is what "the runner is boring" means in practice.

---

## Common confusion: "adapter pattern" vs. our adapters

The Gang-of-Four "adapter pattern" wraps an existing object to fit a new interface. Our adapters do that *and* are the primary extension point. The two meanings overlap by design — every adapter is also adapting some external API to our `SystemAdapter` / `DatasetAdapter` / `TraceStore` Protocol.

If you find a case where an adapter is *not* wrapping something external, it should probably be a plain function or an evaluator instead.
