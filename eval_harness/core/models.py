from __future__ import annotations

import hashlib
import json
import traceback
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, PrivateAttr

from eval_harness.core.time import utc_now


class ExpectedBehavior(BaseModel):
    must_call_tools: list[str] = Field(default_factory=list)
    answer_should_include: list[str] = Field(default_factory=list)
    answer_should_not_include: list[str] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    must_modify_files: list[str] = Field(default_factory=list)
    must_not_modify_files: list[str] = Field(default_factory=list)


class EvalCase(BaseModel):
    schema_version: str = "1.0"
    id: str
    input: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)
    expected: ExpectedBehavior = Field(default_factory=ExpectedBehavior)

    # Private attribute populated by DatasetAdapters that set
    # `embed_full_trace=true` (e.g. langfuse, phoenix, fixture). The replay
    # SystemAdapter unwraps this back into a `Trace` for online evaluation.
    # Private = excluded from serialization; the case JSON stays the same.
    _embedded_trace: Trace | None = PrivateAttr(default=None)


class RunVariant(BaseModel):
    schema_version: str = "1.0"
    name: str
    adapter: str
    config: dict[str, Any]
    metadata: dict[str, Any] = Field(default_factory=dict)


class ToolCall(BaseModel):
    id: str | None = None
    name: str
    arguments: dict[str, Any]
    started_at: datetime | None = None


class ToolResult(BaseModel):
    tool_call_id: str | None = None
    name: str
    content: dict[str, Any] | str


class TraceMessage(BaseModel):
    role: str
    content: str | dict[str, Any] | None = None
    thinking: str | None = None
    tool_call: ToolCall | None = None
    name: str | None = None


class TraceOutput(BaseModel):
    final_answer: str | None = None
    thinking: str | None = None
    structured: dict[str, Any] | None = None


class TraceMetrics(BaseModel):
    token_input: int | None = None
    token_output: int | None = None
    token_thinking: int | None = None
    cost_usd: float | None = None
    cost_thinking_usd: float | None = None
    latency_first_token_ms: int | None = None
    latency_last_token_ms: int | None = None
    tokens_per_second: float | None = None
    stream_chunks: int | None = None
    stream_completed: bool | None = None
    custom: dict[str, Any] = Field(default_factory=dict)


class TraceError(BaseModel):
    type: str
    message: str
    stack: str | None = None


class Trace(BaseModel):
    schema_version: str = "1.0"
    run_id: str
    case_id: str
    variant_name: str
    started_at: datetime
    finished_at: datetime
    latency_ms: int

    input: dict[str, Any]
    output: TraceOutput

    messages: list[TraceMessage] = Field(default_factory=list)
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)

    metrics: TraceMetrics = Field(default_factory=TraceMetrics)
    error: TraceError | None = None
    extra: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_error(cls, case: str, variant: str, type: str, msg: str) -> Trace:
        now = utc_now()
        return cls(
            run_id="",
            case_id=case,
            variant_name=variant,
            started_at=now,
            finished_at=now,
            latency_ms=0,
            input={},
            output=TraceOutput(),
            error=TraceError(type=type, message=msg),
        )


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
    detail: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    finished_at: datetime
    latency_ms: int
    error: TraceError | None = None

    @classmethod
    def from_error(cls, evaluator: str, error: Exception) -> EvaluationResult:
        now = utc_now()
        return cls(
            run_id="",
            case_id="",
            variant_name="",
            evaluator=evaluator,
            evaluator_type="",
            passed=False,
            reason=f"Evaluator '{evaluator}' crashed: {error}",
            started_at=now,
            finished_at=now,
            latency_ms=0,
            error=TraceError(
                type=type(error).__name__,
                message=str(error),
                stack=traceback.format_exc(),
            ),
        )


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


class EvaluatorVariantRollup(BaseModel):
    pass_rate: float
    avg_score: float | None


class EvaluatorRollup(BaseModel):
    evaluator: str
    by_variant: dict[str, EvaluatorVariantRollup]


class VariantDelta(BaseModel):
    variant: str
    pass_rate_delta: float
    avg_latency_delta_ms: float
    regressions: list[str]
    improvements: list[str]


class ComparisonReport(BaseModel):
    baseline: str
    deltas: list[VariantDelta]
    # 'ad_hoc' = within-run variant comparison (the original v0 use).
    # 'drift'  = baseline-run vs current-run comparison (v1.x drift CLI /
    # webhook sink). Default preserves backwards compatibility — existing
    # `summary.yaml` files without this field load as ``kind='ad_hoc'``.
    kind: Literal["ad_hoc", "drift"] = "ad_hoc"
    # Populated only when ``kind == 'drift'``. None for ad_hoc so the
    # field is invisible in the existing v0/v0.1/v0.2/v1 surface.
    baseline_run_id: str | None = None
    regressions_count: int | None = None
    improvements_count: int | None = None


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
    # Multi-sink output: when `output:` lists more than one TraceStore, the
    # first is canonical and failures abort the run; non-first sinks are
    # best-effort mirrors and their failures land here. Additive field;
    # readers that don't know about it keep working. See docs/Observability.md
    # > "Platforms are sources, sinks, and enrichers — never the source of
    # truth."
    sink_errors: list[dict[str, Any]] = Field(default_factory=list)


class FileEntry(BaseModel):
    size: int
    mode: int
    mtime: float
    sha256: str


class FileManifest(BaseModel):
    files: dict[str, FileEntry]


class FileDiff(BaseModel):
    added: list[str]
    removed: list[str]
    modified: list[str]
    text_diffs: dict[str, str] = Field(default_factory=dict)


class FilesystemArtifact(BaseModel):
    schema_version: str = "1.0"
    case_id: str
    variant_name: str
    workspace_kind: str
    before_manifest: FileManifest
    after_manifest: FileManifest
    diff: FileDiff
    artifacts_path: str


class CellDescriptor(BaseModel):
    """v2: the unit of distribution.

    The runner builds a `CellDescriptor` per (case, variant) cell and hands
    it to the `Executor`. Workers REBUILD adapters / evaluators from
    `eval_config_dict` via the existing factory + entry-point layer —
    they do NOT receive serialized adapter instances. The pickle-the-
    function path dies the moment custom-evaluator entry-points enter
    the picture (the v0.1 plugin path), so it's not an option.
    """

    schema_version: str = "1.0"
    cell_id: str
    run_id: str
    case_id: str
    variant_name: str
    # Stable hash over the JSON-canonical config slice that affects this
    # cell (the variant's config + the evaluators touching it). Drives
    # `cell_id` uniqueness and the trace-store idempotency check.
    config_hash: str
    # Full config dict the worker rebuilds adapters from. The worker's
    # factory layer + the entry-point sets in its installed env are
    # responsible for resolving names — config travels, code doesn't.
    eval_config_dict: dict[str, Any]
    # Full `EvalCase.model_dump()`. The worker re-validates via Pydantic.
    case_dict: dict[str, Any]
    workspace_kind: str | None = None
    # Optional capacity-pool name; F2 (Local executor) reads this to
    # route the cell to the matching semaphore. `None` means default pool.
    pool: str | None = None


def compute_cell_id(
    *,
    run_id: str,
    case_id: str,
    variant_name: str,
    config_slice: dict[str, Any],
) -> str:
    """Deterministic `cell_id` for the dispatch primitive.

    Format: ``{run_id}::{case_id}::{variant_name}::{config_hash[:12]}``.
    `config_slice` should contain only the configuration that affects
    this cell — typically the variant block + the evaluator blocks —
    not the whole `eval.yaml`. Same inputs across machines hash to the
    same id, which is what the trace-store idempotency check relies on.
    """
    canonical = json.dumps(config_slice, sort_keys=True, default=str)
    config_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"{run_id}::{case_id}::{variant_name}::{config_hash[:12]}"
