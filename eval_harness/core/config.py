from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

_ALLOW = ConfigDict(extra="allow")
_FORBID = ConfigDict(extra="forbid")


class EvalIdentity(BaseModel):
    model_config = _FORBID
    name: str
    description: str | None = None
    owner: str | None = None
    tags: list[str] = Field(default_factory=list)


class DatasetConfig(BaseModel):
    model_config = _ALLOW
    type: str
    path: str | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    sample: int | None = None


class SystemConfig(BaseModel):
    model_config = _ALLOW
    name: str
    adapter: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = 120
    enrich_trace_from: list[dict[str, Any]] = Field(default_factory=list)
    # v2: optional capacity-pool routing. Absent / None means the
    # variant uses the executor's per-variant semaphore (existing
    # behaviour). When set, the LocalExecutor routes this variant's
    # cells through the matching `run.executor.pools[<name>]` semaphore.
    pool: str | None = None


class WorkspaceConfig(BaseModel):
    model_config = _ALLOW
    type: str
    copy_from: str | None = None


class TraceCaptureConfig(BaseModel):
    model_config = _FORBID
    capture: list[str] = Field(default_factory=list)


class EvaluatorConfig(BaseModel):
    model_config = _FORBID
    name: str
    type: str
    config: dict[str, Any] = Field(default_factory=dict)


class PassCriteria(BaseModel):
    model_config = _FORBID
    all_required: list[str] = Field(default_factory=list)
    any_required: list[str] = Field(default_factory=list)


class RetryPolicy(BaseModel):
    model_config = _FORBID
    max_attempts: int = 1
    on: list[str] = Field(default_factory=list)
    backoff_seconds: float = 0.0


class ExecutorConfig(BaseModel):
    """v2: chooses the dispatch backend. Default is the in-process
    `local` executor — omit the block entirely to keep v0/v0.1/v0.2/v1
    behaviour unchanged.

    `pools` lets a config declare named capacity pools that variants
    reference via `systems[].pool`. Absent pools → existing per-variant
    semaphore behaviour.
    """

    model_config = _ALLOW
    type: str = "local"
    pools: dict[str, int] = Field(default_factory=dict)


class RunOptions(BaseModel):
    model_config = _FORBID
    max_concurrency: int = 4
    per_variant_concurrency: int | None = None
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    baseline_variant: str | None = None
    cost_limit_usd: float | None = None
    executor: ExecutorConfig = Field(default_factory=ExecutorConfig)


class OutputConfig(BaseModel):
    model_config = _ALLOW
    type: str
    path: str | None = None


class MetricsConfig(BaseModel):
    model_config = _FORBID
    price_table_path: str | None = None


class EvalConfig(BaseModel):
    model_config = _FORBID
    schema_version: str = "1.0"
    eval: EvalIdentity
    metadata: dict[str, Any] = Field(default_factory=dict)
    dataset: DatasetConfig
    systems: list[SystemConfig]
    workspace: WorkspaceConfig | None = None
    trace: TraceCaptureConfig | None = None
    evaluators: list[EvaluatorConfig]
    pass_criteria: PassCriteria = Field(default_factory=PassCriteria)
    run: RunOptions = Field(default_factory=RunOptions)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    output: list[OutputConfig]

    @model_validator(mode="before")
    @classmethod
    def _coerce_output_to_list(cls, data: Any) -> Any:
        if isinstance(data, dict):
            out = data.get("output")
            if isinstance(out, dict):
                data = {**data, "output": [out]}
        return data
