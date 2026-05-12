"""Multi-sink output: first store is canonical, rest are best-effort mirrors.

Tests use stub TraceStores (one good, one always-raising) plumbed in through
RunPlan.secondary_trace_stores. See docs/Observability.md > "Platforms are
sources, sinks, and enrichers — never the source of truth."
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Self

import pytest

from eval_harness.core.config import (
    DatasetConfig,
    EvalConfig,
    EvalIdentity,
    OutputConfig,
    SystemConfig,
)
from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    RunVariant,
    Trace,
    TraceOutput,
)
from eval_harness.core.time import utc_now
from eval_harness.runner.plan_builder import RunPlan
from eval_harness.runner.run_eval import run_eval

# --------------------------- fakes ---------------------------


class _GoodStore:
    """Records everything it sees; never raises."""

    def __init__(self, label: str = "good") -> None:
        self.label = label
        self.traces: list[Trace] = []
        self.results: list[EvaluationResult] = []
        self.summary: RunSummary | None = None
        self.opened = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def open(self, run_id: str, run_dir: Path) -> None:
        self.opened = True

    async def save_trace(self, trace: Trace) -> None:
        self.traces.append(trace)

    async def save_evaluation(
        self, case_id: str, variant: str, results: list[EvaluationResult]
    ) -> None:
        self.results.extend(results)

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        return None

    async def save_summary(self, summary: RunSummary) -> None:
        self.summary = summary

    async def close(self) -> None:
        return None


class _AlwaysRaisingStore:
    """Raises on every operation. Used to assert secondary failures don't
    bring the run down."""

    def __init__(self, label: str = "broken") -> None:
        self.label = label

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def open(self, run_id: str, run_dir: Path) -> None:
        raise RuntimeError("open boom")

    async def save_trace(self, trace: Trace) -> None:
        raise RuntimeError("save_trace boom")

    async def save_evaluation(
        self, case_id: str, variant: str, results: list[EvaluationResult]
    ) -> None:
        raise RuntimeError("save_evaluation boom")

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        raise RuntimeError("save_artifact boom")

    async def save_summary(self, summary: RunSummary) -> None:
        raise RuntimeError("save_summary boom")

    async def close(self) -> None:
        return None


class _StubAdapter:
    def __init__(self, name: str) -> None:
        self.name = name

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def run(
        self, case: EvalCase, variant: RunVariant, workspace: object
    ) -> Trace:
        now = utc_now()
        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=now,
            finished_at=now,
            latency_ms=0,
            input=dict(case.input),
            output=TraceOutput(final_answer=f"answer-for-{case.id}"),
        )


# --------------------------- config-level tests ---------------------------


_MIN_CONFIG = {
    "schema_version": "1.0",
    "eval": {"name": "t"},
    "dataset": {"type": "yaml", "path": "x.yaml"},
    "systems": [{"name": "v1", "adapter": "python_function"}],
    "evaluators": [],
}


def test_single_mapping_output_still_validates() -> None:
    """v0 form: `output:` is a mapping. Must coerce to a one-element list."""
    cfg = EvalConfig.model_validate(
        {**_MIN_CONFIG, "output": {"type": "local_files", "path": "./runs"}}
    )
    assert isinstance(cfg.output, list)
    assert len(cfg.output) == 1
    assert cfg.output[0].type == "local_files"
    assert cfg.output[0].path == "./runs"


def test_list_output_validates() -> None:
    cfg = EvalConfig.model_validate(
        {
            **_MIN_CONFIG,
            "output": [
                {"type": "local_files", "path": "./runs"},
                {"type": "sqlite", "path": "./runs/eval.sqlite"},
                {"type": "langfuse", "host": "https://example.com"},
            ],
        }
    )
    assert [o.type for o in cfg.output] == ["local_files", "sqlite", "langfuse"]


def test_empty_output_list_rejected_by_plan_builder() -> None:
    """The plan builder still rejects `output: []` (no canonical sink)."""
    cfg = EvalConfig.model_validate({**_MIN_CONFIG, "output": []})

    # Re-import build_plan inline so we don't pull the rest of the test file
    # into the import graph during collection.
    import asyncio

    from eval_harness.runner.plan_builder import build_plan

    with pytest.raises(ConfigError, match="output"):
        asyncio.run(build_plan(cfg, Path("dummy/eval.yaml")))


# --------------------------- plan-builder tests ---------------------------


async def test_plan_builder_builds_n_stores_first_is_canonical(
    tmp_path: Path,
) -> None:
    """plan_builder.build N stores when output is a list; first is canonical."""
    from eval_harness.factories.dataset_adapter_factory import DatasetAdapterFactory
    from eval_harness.factories.evaluator_factory import EvaluatorFactory
    from eval_harness.factories.system_adapter_factory import SystemAdapterFactory
    from eval_harness.factories.trace_enricher_factory import TraceEnricherFactory
    from eval_harness.factories.trace_store_factory import TraceStoreFactory
    from eval_harness.factories.workspace_factory import WorkspaceFactory
    from eval_harness.runner.plan_builder import Factories, build_plan

    cases_yaml = tmp_path / "cases.yaml"
    cases_yaml.write_text(
        'schema_version: "1.0"\ndataset:\n  name: x\ncases:\n  - id: c1\n    input: {}\n'
    )

    class _StoreA:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            self.label = "A"
            self.path = kwargs.get("path")

    class _StoreB:
        def __init__(self, *_args: object, **kwargs: object) -> None:
            self.label = "B"
            self.path = kwargs.get("path")

    # Self-contained factory set so we don't touch the singletons.
    facs = Factories(
        dataset=DatasetAdapterFactory(),
        system=SystemAdapterFactory(),
        evaluator=EvaluatorFactory(),
        workspace=WorkspaceFactory(),
        trace_store=TraceStoreFactory(),
        trace_enricher=TraceEnricherFactory(),
    )
    # Re-use the real yaml dataset adapter so build_plan can load the cases.
    from eval_harness.adapters.dataset.yaml_dataset_adapter import YamlDatasetAdapter

    facs.dataset.register("yaml", YamlDatasetAdapter)
    facs.system.register("stub_sys", lambda name, **_cfg: _StubAdapter(name))
    facs.trace_store.register("stub_a", _StoreA)
    facs.trace_store.register("stub_b", _StoreB)

    cfg = EvalConfig.model_validate(
        {
            "schema_version": "1.0",
            "eval": {"name": "multi"},
            "dataset": {"type": "yaml", "path": str(cases_yaml)},
            "systems": [{"name": "v1", "adapter": "stub_sys"}],
            "evaluators": [],
            "output": [
                {"type": "stub_a", "path": str(tmp_path / "runs_a")},
                {"type": "stub_b", "path": str(tmp_path / "runs_b")},
            ],
        }
    )
    plan = await build_plan(cfg, tmp_path / "eval.yaml", factories=facs)

    assert plan.trace_store.__class__.__name__ == "_StoreA"
    assert len(plan.secondary_trace_stores) == 1
    assert plan.secondary_trace_stores[0].__class__.__name__ == "_StoreB"
    # Canonical owns the on-disk run_dir.
    assert plan.run_dir.parent == tmp_path / "runs_a"


# --------------------------- runner-level tests ---------------------------


def _make_config() -> EvalConfig:
    return EvalConfig(
        schema_version="1.0",
        eval=EvalIdentity(name="t"),
        dataset=DatasetConfig(type="yaml"),
        systems=[SystemConfig(name="v1", adapter="stub")],
        evaluators=[],
        output=[OutputConfig(type="local_files", path="./runs")],
    )


def _make_plan(
    *,
    primary: _GoodStore | _AlwaysRaisingStore,
    secondaries: list[object],
) -> RunPlan:
    cfg = _make_config()
    return RunPlan(
        config=cfg,
        run_id="multi-run",
        run_dir=Path("./runs/multi-run"),
        cases=[EvalCase(id="c1", input={"q": 1})],
        variants=[RunVariant(name="v1", adapter="stub", config={})],
        system_adapters={"v1": _StubAdapter("v1")},
        trace_store=primary,  # type: ignore[arg-type]
        workspace=None,
        evaluators=[],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
        secondary_trace_stores=secondaries,  # type: ignore[arg-type]
    )


async def test_first_sink_failure_aborts_run() -> None:
    """Canonical failure aborts — the contract that local_files is the source
    of truth (per docs/Observability.md). The first op that touches the
    canonical (open) is enough to bring the run down."""
    plan = _make_plan(primary=_AlwaysRaisingStore("primary"), secondaries=[])
    with pytest.raises(RuntimeError, match="boom"):
        await run_eval(plan)


async def test_secondary_sink_failure_records_into_summary() -> None:
    """Secondary failures are best-effort: per-op error rows accumulate in
    RunSummary.sink_errors; the run completes."""
    good = _GoodStore("primary")
    bad = _AlwaysRaisingStore("mirror")
    plan = _make_plan(primary=good, secondaries=[bad])
    summary = await run_eval(plan)

    # Canonical still saw everything.
    assert len(good.traces) == 1
    assert good.summary is summary

    # Mirror's failures landed on the summary, NOT in the raised exception.
    sinks = summary.sink_errors
    assert any(e["op"] == "open" for e in sinks)
    assert any(e["op"] == "save_trace" for e in sinks)
    assert any(e["op"] == "save_summary" for e in sinks)
    for entry in sinks:
        assert entry["sink"] == "_AlwaysRaisingStore"
        assert "boom" in entry["error"]


async def test_multiple_secondaries_each_get_called_and_only_bad_one_logs() -> None:
    """When two mirrors are wired, the good one sees writes and the broken
    one logs into sink_errors. Order of secondaries is preserved."""
    primary = _GoodStore("primary")
    good_mirror = _GoodStore("mirror_a")
    bad_mirror = _AlwaysRaisingStore("mirror_b")
    plan = _make_plan(
        primary=primary, secondaries=[good_mirror, bad_mirror]
    )
    summary = await run_eval(plan)

    # Both good stores saw the trace.
    assert len(primary.traces) == 1
    assert len(good_mirror.traces) == 1

    # Only the broken mirror produced sink_errors entries.
    sink_names = {e["sink"] for e in summary.sink_errors}
    assert sink_names == {"_AlwaysRaisingStore"}


async def test_no_secondaries_means_no_sink_errors() -> None:
    primary = _GoodStore("primary")
    plan = _make_plan(primary=primary, secondaries=[])
    summary = await run_eval(plan)
    assert summary.sink_errors == []


async def test_summary_sink_errors_field_round_trips() -> None:
    """The new field is part of the pydantic model; model_dump preserves it."""
    summary = RunSummary(
        run_id="r1",
        started_at=utc_now(),
        finished_at=utc_now(),
        config_path="x.yaml",
        config_hash="",
        cases_total=0,
        variants=[],
        by_evaluator=[],
        sink_errors=[{"sink": "X", "op": "save_trace", "error": "boom"}],
    )
    rebuilt = RunSummary.model_validate(summary.model_dump(mode="json"))
    assert rebuilt.sink_errors == [
        {"sink": "X", "op": "save_trace", "error": "boom"}
    ]
