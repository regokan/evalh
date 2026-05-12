"""LocalExecutor unit tests.

Lifecycle + Protocol-conformance + semaphore routing on a small
synthetic plan. Concurrency / failure-isolation regression coverage
lives in the existing `tests/unit/test_runner.py` suite (which now
runs through LocalExecutor end-to-end via the runner).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Self

from eval_harness.core.executors import Executor
from eval_harness.core.executors.local import LocalExecutor
from eval_harness.core.models import (
    CellDescriptor,
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    RunVariant,
    Trace,
    TraceOutput,
)
from eval_harness.core.time import utc_now


class _FakeAdapter:
    name = "fake"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def run(
        self, case: EvalCase, variant: RunVariant, workspace: Any
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
            output=TraceOutput(final_answer="ok"),
        )


class _FakeStore:
    def __init__(self) -> None:
        self.traces: list[Trace] = []

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def open(self, run_id: str, run_dir: Path) -> None:
        return None

    async def save_trace(self, trace: Trace) -> None:
        self.traces.append(trace)

    async def save_evaluation(
        self, case_id: str, variant: str, results: list[EvaluationResult]
    ) -> None:
        return None

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        return None

    async def save_summary(self, summary: RunSummary) -> None:
        return None

    async def close(self) -> None:
        return None


def _plan(tmp_path: Path) -> Any:
    from eval_harness.core.config import (
        DatasetConfig,
        EvalConfig,
        EvalIdentity,
        EvaluatorConfig,
        OutputConfig,
        PassCriteria,
        RetryPolicy,
        RunOptions,
        SystemConfig,
    )
    from eval_harness.evaluators.base import Evaluator
    from eval_harness.runner.plan_builder import RunPlan

    class _NoopEval(Evaluator):
        type = "noop"

        async def evaluate(self, case, trace, artifact):  # type: ignore[no-untyped-def]
            return EvaluationResult(
                run_id=trace.run_id,
                case_id=case.id,
                variant_name=trace.variant_name,
                evaluator=self.name,
                evaluator_type=self.type,
                passed=True,
                reason="ok",
                started_at=utc_now(),
                finished_at=utc_now(),
                latency_ms=0,
            )

    cfg = EvalConfig(
        eval=EvalIdentity(name="executor-unit"),
        dataset=DatasetConfig(type="yaml", path="x"),
        systems=[SystemConfig(name="v1", adapter="fake")],
        evaluators=[EvaluatorConfig(name="noop", type="noop")],
        pass_criteria=PassCriteria(),
        run=RunOptions(max_concurrency=2, retry=RetryPolicy()),
        output=[OutputConfig(type="local_files", path="./runs")],
    )
    cases = [EvalCase(id="c1", input={"q": "hi"})]
    variants = [RunVariant(name="v1", adapter="fake", config={})]
    return RunPlan(
        config=cfg,
        run_id="r-unit",
        run_dir=tmp_path / "runs" / "r-unit",
        cases=cases,
        variants=variants,
        system_adapters={"v1": _FakeAdapter()},
        trace_store=_FakeStore(),
        workspace=None,
        evaluators=[_NoopEval(name="noop")],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
    )


def test_local_executor_satisfies_protocol() -> None:
    assert isinstance(LocalExecutor(), Executor)


async def test_local_executor_full_lifecycle(tmp_path: Path) -> None:
    """open -> submit_cell -> await_outcome -> close. Round-trips a
    single cell through the Protocol surface (not the fast path)."""
    plan = _plan(tmp_path)
    executor = LocalExecutor()
    async with executor:
        await executor.open(plan)
        # Bind one cell against the live (case, variant) pair.
        cell = CellDescriptor.model_construct(
            schema_version="1.0",
            cell_id="r-unit::c1::v1::deadbeef0000",
            run_id="r-unit",
            case_id="c1",
            variant_name="v1",
            config_hash="deadbeef0000",
            eval_config_dict={},
            case_dict={},
            workspace_kind=None,
            pool=None,
        )
        executor.bind_cells([(cell, plan.cases[0], plan.variants[0])])
        handle = await executor.submit_cell(cell)
        outcome = await executor.await_outcome(handle)
        assert outcome.case.id == "c1"
        assert outcome.trace.output.final_answer == "ok"
        summary = executor.finalize()
        assert summary.variants[0].cases_passed == 1


async def test_local_executor_await_all(tmp_path: Path) -> None:
    """The bulk-await path also works on a small batch."""
    plan = _plan(tmp_path)
    executor = LocalExecutor()
    async with executor:
        await executor.open(plan)
        cell = CellDescriptor.model_construct(
            schema_version="1.0",
            cell_id="r-unit::c1::v1::aaaaaaaaaaaa",
            run_id="r-unit",
            case_id="c1",
            variant_name="v1",
            config_hash="aaaaaaaaaaaa",
            eval_config_dict={},
            case_dict={},
            workspace_kind=None,
            pool=None,
        )
        executor.bind_cells([(cell, plan.cases[0], plan.variants[0])])
        handles = [await executor.submit_cell(cell)]
        outcomes = await executor.await_all(handles)
        assert len(outcomes) == 1


async def test_local_executor_fast_path_dispatch_all(tmp_path: Path) -> None:
    """The fast path skips CellDescriptor construction entirely — the
    runner uses it on the local route."""
    plan = _plan(tmp_path)
    executor = LocalExecutor()
    async with executor:
        await executor.open(plan)
        outcomes = await executor.dispatch_all_local(
            [(plan.cases[0], plan.variants[0], None)]
        )
        assert len(outcomes) == 1
        assert outcomes[0].case.id == "c1"


async def test_local_executor_pool_routing(tmp_path: Path) -> None:
    """Pool name resolves via `_semaphore_for(cell, variant)` when a
    cell carries `pool='fast'`."""
    plan = _plan(tmp_path)
    executor = LocalExecutor(pools={"fast": 2})
    async with executor:
        await executor.open(plan)
        cell = CellDescriptor.model_construct(
            schema_version="1.0",
            cell_id="r-unit::c1::v1::bbbbbbbbbbbb",
            run_id="r-unit",
            case_id="c1",
            variant_name="v1",
            config_hash="bbbbbbbbbbbb",
            eval_config_dict={},
            case_dict={},
            workspace_kind=None,
            pool="fast",
        )
        sem = executor._semaphore_for(cell, plan.variants[0])
        assert sem is executor._pool_semaphores["fast"]


async def test_local_executor_unknown_pool_falls_back_to_variant_semaphore(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    executor = LocalExecutor(pools={})
    async with executor:
        await executor.open(plan)
        cell = CellDescriptor.model_construct(
            schema_version="1.0",
            cell_id="r-unit::c1::v1::cccccccccccc",
            run_id="r-unit",
            case_id="c1",
            variant_name="v1",
            config_hash="cccccccccccc",
            eval_config_dict={},
            case_dict={},
            workspace_kind=None,
            pool="nonexistent",
        )
        sem = executor._semaphore_for(cell, plan.variants[0])
        assert sem is executor._variant_semaphores["v1"]


def test_factory_registers_local_executor() -> None:
    from eval_harness.core.executors import executor_registry

    executor_registry.load_entry_points()
    assert "local" in executor_registry.names()


async def test_local_executor_finalize_returns_summary_with_sink_errors(
    tmp_path: Path,
) -> None:
    plan = _plan(tmp_path)
    executor = LocalExecutor()
    async with executor:
        await executor.open(plan)
        await executor.dispatch_all_local(
            [(plan.cases[0], plan.variants[0], None)]
        )
        summary = executor.finalize()
        # sink_errors is empty on the happy path.
        assert summary.sink_errors == []
        assert summary.cases_total == 1
