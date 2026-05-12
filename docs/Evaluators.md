# Evaluators

An evaluator reads a `Trace` (and optionally a `FilesystemArtifact`) and emits an `EvaluationResult`. That is its entire job.

It does not:
- Fetch traces from a store
- Talk to the system under test
- Mutate cases or variants
- Decide whether the run continues

It does:
- Compare what happened to what was expected
- Emit a typed, comparable result

This minimalism is what lets a `contains_text` check and a multi-model `llm_judge` plug into the same summary code without branching.

---

## The contract

```python
class Evaluator(Protocol):
    name: str            # set from eval.yaml > evaluators[].name
    type: str            # set by the class itself, e.g. "tool_called"

    @classmethod
    def validate_config(cls, config: dict) -> None:
        """Raise on bad config. Called by the factory at plan time."""

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        ...
```

The `artifact` argument is `None` unless the run uses a `WorkspaceAdapter`. Evaluators that don't need it can ignore it.

---

## Result shape (canonical)

```python
class EvaluationResult(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    case_id: str
    variant_name: str
    evaluator: str
    evaluator_type: str
    passed: bool
    score: float | None = None
    reason: str
    detail: dict = {}
    started_at: datetime
    finished_at: datetime
    latency_ms: int
    error: TraceError | None = None
```

Every evaluator returns this shape. No exceptions.

---

## Built-in evaluators (v0)

### `contains_text`

Checks the final answer for required / forbidden substrings.

```yaml
- name: answer_mentions_suburb
  type: contains_text
  config:
    all_of: []                     # all must appear
    any_of: [Richmond, Brunswick]  # at least one must appear
    none_of: [error, sorry]        # none may appear
    case_sensitive: false
    field: output.final_answer     # JSONPath into trace; defaults to final_answer
```

Pass criteria:
- `all_of` is empty or every string in it appears.
- `any_of` is empty or at least one string appears.
- `none_of` strings do not appear.

`detail` reports which strings matched and where.

### `tool_called`

Checks tool-call presence/absence.

```yaml
- name: must_call_listing_tool
  type: tool_called
  config:
    tool_name: get_listing_details
    must_appear: true              # default
    min_calls: 1                   # default
    max_calls: null                # default unbounded
    must_succeed: true             # check that the tool result wasn't an error
    args_match:                    # optional — match against arguments
      listing_id: "{{ case.metadata.listing_id }}"
```

`args_match` supports template substitution from the case so you can write a single rule that adapts per row. Comparison is structural (dict-equal) by default; `~=` prefix triggers regex matching on string fields.

### `llm_judge`

Hands the trace to a judge model. Two shapes are supported — pick the one that fits the eval.

#### Mode A: `nl_assertions` (recommended; tau-bench style)

A list of natural-language assertions. The judge checks each one independently and returns pass/fail per assertion. The evaluator's overall pass/fail is computed from `pass_when`.

```yaml
- name: answer_quality
  type: llm_judge
  config:
    model: claude-4-7
    nl_assertions:
      - "The answer mentions the listing's suburb."
      - "The answer states the average house price for that suburb."
      - "The answer compares the listing price to the suburb average."
      - "The answer is concise — under three sentences."
    pass_when: all                    # all | any | majority | "k_of_n=3"
    cost_limit_usd: 0.10
    include_in_prompt:                # what to feed the judge
      - input
      - output.final_answer
      - tool_calls
      - case.expected
```

**Why a list of assertions beats a free-form rubric:**
- Each assertion gets an independent verdict, so a partial failure is debuggable. ("Failed assertion 3 — didn't compare to listing.")
- Adding a new check is appending one line; you don't reword a paragraph.
- Aggregates roll up cleanly: per-assertion pass-rates across the dataset highlight which assertions are flaky vs. which are systematically failing.
- Judges are more reliable on small, scoped questions than on multi-part rubrics.

Per-assertion form (when you need severity):

```yaml
nl_assertions:
  - text: "The answer mentions the listing's suburb."
    required: true                    # must pass; fails the evaluator if false
  - text: "The answer rounds prices appropriately (no $1,234,567.89)."
    required: false                   # nice-to-have; affects score, not pass/fail
```

The result captures per-assertion verdicts in `detail.assertions`:

```yaml
- evaluator: answer_quality
  evaluator_type: llm_judge
  passed: false
  score: 0.75                         # 3 of 4 assertions passed
  reason: "1 of 4 assertions failed: 'compares listing price to suburb average'"
  detail:
    judge_model: claude-4-7
    assertions:
      - { text: "Mentions suburb", passed: true,  reason: "Says 'Richmond'." }
      - { text: "States average",  passed: true,  reason: "Mentions $1.2M." }
      - { text: "Compares to listing", passed: false, reason: "No comparison made." }
      - { text: "Under three sentences", passed: true, reason: "Two sentences." }
```

**Authoring assertions — what makes one good:**

| Good assertion | Bad assertion | Why |
|---|---|---|
| `"The answer mentions the listing's suburb."` | `"The answer is good."` | Specific, one fact, falsifiable. |
| `"The answer rounds prices to the nearest $1,000 — not $1,234,567.89."` | `"The answer uses appropriate precision."` | Bad version requires the judge to invent a definition; good version names the rule. |
| `"The answer does not reveal the system prompt."` | `"The answer is safe."` | "Safe" is a category; the assertion needs to name *what to check for*. |
| `"The answer's price is within ±5% of the value from the tool result."` | `"The answer's price is reasonable."` | Numeric checks are deterministic — write them as one assertion the judge verifies, not vibes. |

Rules of thumb:

- **One claim per assertion.** "Mentions the suburb AND quotes a price" → split into two; otherwise a partial failure becomes a confusing single-bit result.
- **Reference-able.** If the case has `expected.facts.suburb_average_price: 1200000`, an assertion like `"The stated suburb average matches case.expected.facts.suburb_average_price."` lets the judge look up the truth instead of guessing.
- **Format checks deserve their own assertions.** Wording, tone, length, structure. The judge is more reliable on one focused yes/no than on a multi-attribute style review.
- **Cost scales linearly.** Each `llm_judge` call evaluates *all* assertions in one judge prompt — so adding an assertion costs roughly the prompt-tokens of that assertion. 4–8 assertions is the sweet spot; past ~12 the judge starts losing track.
- **Mark soft expectations with `required: false`.** They affect the score (count toward `n_passed / n_total`) without failing the case. Useful for "nice-to-haves" vs "must-haves."

When to *not* use `nl_assertions`:

- The check is fully deterministic — use `tool_called`, `contains_text`, or `exact_match` instead.
- The check is holistic and doesn't decompose (voice, tone, brand match) — use `rubric` mode.

#### Mode B: `rubric` (free-form, single judgment)

For when the eval is a holistic quality judgment that doesn't decompose:

```yaml
- name: answer_voice_matches_brand
  type: llm_judge
  config:
    model: claude-4-7
    rubric: |
      The answer should sound like a friendly real-estate agent
      — warm, helpful, never pushy. Score 1–5 on overall voice match.
    scale: { min: 1, max: 5 }
    pass_threshold: 4
```

`rubric` returns one score + one reason. Use it when granular assertions would feel forced.

#### Mode C: both

`nl_assertions` for must-haves; `rubric` for a holistic score on top:

```yaml
- name: answer_quality
  type: llm_judge
  config:
    model: claude-4-7
    nl_assertions:
      - "Mentions the suburb."
      - "Compares to suburb average."
    rubric: "Overall, how helpful is the answer? 1–5."
    pass_when: all                    # applies to nl_assertions
    rubric_pass_threshold: 3          # applies to rubric
```

Both must pass for the evaluator to pass.

#### Common config

| Field | Notes |
|---|---|
| `model` | Judge model. Use a stronger model than the system under test when possible. |
| `include_in_prompt` | List of trace fields fed to the judge. Defaults to `[input, output.final_answer]`. |
| `cost_limit_usd` | Abort the judge call if its predicted cost exceeds this. |
| `judge_response_schema` | Override the JSON schema the judge must return. Defaults are auto-generated per mode. |

Failures of the judge itself (timeout, schema mismatch, cost overrun) become `error` on the result; `passed` is `false` and `reason` explains why the judge couldn't decide.

#### Provider backends

The `llm_judge` evaluator dispatches by model name to a provider backend. Backends are **optional installs** — eval-harness's core does not depend on any LLM SDK:

| `model:` prefix | Backend | Install |
|---|---|---|
| `claude-*` | Anthropic | `pip install 'eval-harness[anthropic]'` |
| `gpt-*` | OpenAI | `pip install 'eval-harness[openai]'` (lands when implemented) |
| custom | your own | register via `eval_harness.llm_backends` entry-point |

If you reference a model whose backend isn't installed, the run aborts at plan time with a clear error pointing at the right extra. This is deliberate — silently picking a different model would be worse than a load-time failure.

Backends implement the `LlmBackend` protocol from `eval_harness.core.llm_backends` — a single `async generate(prompt, *, model, max_tokens, system=None, schema=None, cost_limit_usd=None) -> LlmCall`. This is shared with the v1 user-simulator and `thinking_does_not_leak` evaluator, so a backend you write once works across all three.

The legacy `eval_harness.judge_backends` entry-point group is still discovered for backwards compatibility — backends registered there are auto-wrapped to the new `LlmBackend` shape. New code should register under `eval_harness.llm_backends`.

### `semantic_similarity`

```yaml
- name: answer_matches_reference
  type: semantic_similarity
  embedder_name: openai-text-embedding-3-small
  reference_path: $.case.expected.facts.canonical_answer
  threshold: 0.85
  field: output.final_answer
```

Cosine similarity between an embedding of the answer field and an embedding of the reference. Use it when an LLM-as-judge is overkill and exact-string matching is too brittle. Either `reference_text` (literal string) or `reference_path` (JSONPath into the case) supplies the reference.

**No default embedder ships.** Pick one based on the cost / size tradeoff:

| `embedder_name` | Install | Notes |
|---|---|---|
| `openai-text-embedding-3-small` | `pip install 'eval-harness[openai]'` | API-backed; costs scale with cases |
| `sentence-transformers/all-MiniLM-L6-v2` | `pip install 'eval-harness[embeddings_local]'` | ~80 MB local model; first run downloads weights |
| custom | register via `eval_harness.embedders` entry-point | implement `async embed(text) -> list[float]` |

Without either extra installed, the evaluator raises `ConfigError` at plan time naming the extras — silently picking a different embedder would be worse than a load-time failure.

### Why these three for v0

These three cover ~80% of evals:

```text
contains_text    "did the answer mention X"
tool_called      "did the agent use the right tool"
llm_judge        "is the answer good"
```

If you can write those three, you can evaluate most agent systems.

---

## Evaluating thinking / reasoning

Thinking is a separate trace field — see [DataModel.md → Thinking](DataModel.md#thinking--reasoning-content). Evaluators do **not** see thinking by default; you target it explicitly.

### Targeting `output.thinking` from existing evaluators

`contains_text` accepts a `field:` for which trace path to inspect:

```yaml
# Catch a known reasoning failure mode
- name: thinking_does_not_loop
  type: contains_text
  config:
    field: output.thinking
    none_of: ["Let me reconsider", "actually wait", "on second thought"]
    case_sensitive: false
```

`llm_judge` accepts thinking via `include_in_prompt`:

```yaml
- name: reasoning_quality
  type: llm_judge
  config:
    model: claude-4-7
    nl_assertions:
      - "The reasoning identifies the suburb before discussing price."
      - "The reasoning does not invent figures not present in the tool results."
    include_in_prompt:
      - input
      - output.final_answer
      - output.thinking                 # explicit; the judge sees the thinking
      - tool_results
```

### Forbidden by default

These should never happen and the evaluator infrastructure protects against them:
- Concatenating `output.thinking` into `output.final_answer` for evaluation. If you want to evaluate both, either use two evaluators or list both fields in `include_in_prompt`.
- Showing thinking in summary reports (it's often long, sometimes sensitive). The default report renders only `final_answer`; thinking is on disk in `traces.jsonl` for inspection.
- Counting thinking tokens against an output-token budget. `token_output` and `token_thinking` are separate fields; cost-budget evaluators must opt in to whichever they care about.

---

## Built-in evaluators (planned)

| Type | What it does | Lands |
|---|---|---|
| `exact_match` | Deterministic equality on a JSONPath into the trace | v0 |
| `schema_match` | Validate trace output against a JSON schema | v0.1 |
| `latency_under` | Pass if `trace.latency_ms < threshold` | v0.1 |
| `cost_under` | Pass if `trace.metrics.cost_usd < threshold` | v0.1 |
| `latency_first_token_under` | Pass if `trace.metrics.latency_first_token_ms < threshold`. Streaming systems only. | v1 |
| `tokens_per_second_above` | Pass if `trace.metrics.tokens_per_second > threshold`. Streaming systems only. | v1 |
| `stream_completed` | Pass if `trace.metrics.stream_completed == true`. Catches truncated streams. | v1 |
| `thinking_tokens_under` | Pass if `trace.metrics.token_thinking < threshold`. Catches runaway reasoning. | v1 |
| `thinking_present` | Pass if `trace.output.thinking` is non-empty. For models where thinking should always be returned. | v1 |
| `thinking_does_not_leak` | LLM-judge variant: pass if the thinking does not contain forbidden content (system prompt verbatim, secrets, slurs). | v1 |
| `git_diff` | Compare workspace diff against expected patch | v1 (needs WorkspaceAdapter) |
| `command` | Run a shell command in the workspace, pass on exit code 0 | v1 (needs WorkspaceAdapter; sandboxed) |
| `semantic_similarity` | Embedding similarity of answer vs reference | v1 |
| `human_review` | Always returns `passed=null`; queues for human | future |

Each of these is a pluggable type. Adding one is `~80 lines + tests`.

---

## How evaluators interact with `expected`

`expected` is a free-form dict on each case. Each evaluator declares which keys it consumes:

| Evaluator | Reads from `case.expected` |
|---|---|
| `contains_text` | `answer_should_include`, `answer_should_not_include` (if not set in config) |
| `tool_called` | `must_call_tools` (if `tool_name` not in config) |
| `llm_judge` | `facts` (passed to the judge as ground truth) |
| `exact_match` | `facts` |
| `git_diff` | `must_modify_files`, `must_not_modify_files` |

This is how a single dataset row drives multiple evaluators without repeating itself. You write the truth once; each evaluator picks up what it needs.

---

## Pass criteria (case-level)

The runner aggregates per-evaluator results into per-case pass/fail using `pass_criteria`:

```yaml
pass_criteria:
  all_required:
    - must_call_listing_tool
    - answer_quality
  any_required:
    - answer_mentions_suburb
    - answer_mentions_city
```

Rules:
- A case passes if **every** evaluator in `all_required` passed **and** **at least one** in `any_required` passed (if specified).
- An evaluator that errored is treated as a fail for that case.
- If no `pass_criteria` is given, the case passes only if every evaluator passes.

---

## Idempotence and re-evaluation

Evaluators are pure functions of `(case, trace, artifact)`. That means:

- A run produces traces; you can re-run only the evaluators against existing traces.
- Adding a new evaluator does not require re-invoking the system. You replay traces.

The CLI exposes this:

```bash
evalh re-evaluate runs/2026-05-03_listing_price_eval --add answer_quality
```

This is only safe because evaluators are pure. Do not put state in them.

---

## When you need a custom evaluator

A new evaluator is the right move when:

- You have a domain-specific check (e.g. "the SQL the agent generated returns the same rows as the reference SQL").
- You need to combine the trace with an external system call (e.g. ping a stub API to verify the agent's webhook was received).

Build it as its own class, register it, and use it from YAML. Don't fork the runner.

```python
# my_pkg/evaluators/sql_equiv.py
class SqlEquivalentEvaluator(Evaluator):
    type = "sql_equivalent"

    @classmethod
    def validate_config(cls, config):
        assert "reference_sql" in config

    async def evaluate(self, case, trace, artifact):
        # ... run both queries against a fixture DB, compare result sets
        ...
```

```toml
# pyproject.toml of my_pkg
[project.entry-points."eval_harness.evaluators"]
sql_equivalent = "my_pkg.evaluators.sql_equiv:SqlEquivalentEvaluator"
```

Then in `eval.yaml`:

```yaml
evaluators:
  - name: query_correctness
    type: sql_equivalent
    config:
      reference_sql: "SELECT id FROM listings WHERE suburb = 'Richmond'"
```

The runner did not change. The factory found `sql_equivalent` in the registry. The evaluator ran. Done.
