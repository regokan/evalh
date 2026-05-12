"""Capacity pool integration test for the v2 LocalExecutor.

Two named pools (`fast: 4`, `slow: 1`) with two variants assigned to
them. A deterministic mock adapter records concurrent-call counts per
variant; the test asserts pool-targeted limits hold per pool, not
globally.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from eval_harness.core.config import (
    DatasetConfig,
    EvalConfig,
    EvalIdentity,
    EvaluatorConfig,
    ExecutorConfig,
    OutputConfig,
    PassCriteria,
    RetryPolicy,
    RunOptions,
    SystemConfig,
)
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
from eval_harness.evaluators.base import Evaluator
from eval_harness.runner.plan_builder import RunPlan
from eval_harness.runner.run_eval import run_eval


class _ConcurrencyRecordingAdapter:
    """Tracks the peak number of concurrent `run()` calls. Used to verify
    pool-targeted concurrency limits."""

    def __init__(self, *, sleep_s: float = 0.05) -> None:
        self.name = "rec"
        self.sleep_s = sleep_s
        self.in_flight = 0
        self.peak_in_flight = 0
        self._lock = asyncio.Lock()

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
        self, case: EvalCase, variant: RunVariant, workspace: Any
    ) -> Trace:
        async with self._lock:
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.sleep_s)
        finally:
            async with self._lock:
                self.in_flight -= 1
        now = utc_now()
        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=now,
            finished_at=now,
            latency_ms=int(self.sleep_s * 1000),
            input=dict(case.input),
            output=TraceOutput(final_answer="ok"),
        )


class _NoopEvaluator(Evaluator):
    type = "noop"

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        now = utc_now()
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=True,
            reason="ok",
            started_at=now,
            finished_at=now,
            latency_ms=0,
        )


class _CapturingStore:
    def __init__(self) -> None:
        self.traces: list[Trace] = []
        self.summary: RunSummary | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_: object) -> None:
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
        self.summary = summary

    async def close(self) -> None:
        return None


async def test_pools_separated(tmp_path: Path) -> None:
    """Two pools, two variants. Each variant has 16 cases; fast pool
    allows up to 4 concurrent; slow pool allows 1. Peak in-flight per
    variant must respect its pool's cap, not the global
    `max_concurrency`."""
    fast_adapter = _ConcurrencyRecordingAdapter(sleep_s=0.05)
    slow_adapter = _ConcurrencyRecordingAdapter(sleep_s=0.05)

    cases = [EvalCase(id=f"c{i}", input={"q": i}) for i in range(16)]
    variants = [
        RunVariant(name="fast_v", adapter="fake", config={}),
        RunVariant(name="slow_v", adapter="fake", config={}),
    ]
    cfg = EvalConfig(
        eval=EvalIdentity(name="pools-test"),
        dataset=DatasetConfig(type="yaml", path="x"),
        systems=[
            SystemConfig(name="fast_v", adapter="fake", pool="fast"),
            SystemConfig(name="slow_v", adapter="fake", pool="slow"),
        ],
        evaluators=[EvaluatorConfig(name="noop", type="noop")],
        pass_criteria=PassCriteria(),
        run=RunOptions(
            max_concurrency=32,
            retry=RetryPolicy(),
            executor=ExecutorConfig(type="local", pools={"fast": 4, "slow": 1}),
        ),
        output=[OutputConfig(type="local_files", path="./runs")],
    )
    store = _CapturingStore()
    plan = RunPlan(
        config=cfg,
        run_id="r1",
        run_dir=tmp_path / "runs" / "r1",
        cases=cases,
        variants=variants,
        system_adapters={"fast_v": fast_adapter, "slow_v": slow_adapter},
        trace_store=store,
        workspace=None,
        evaluators=[_NoopEvaluator(name="noop")],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
    )

    started = time.monotonic()
    summary = await run_eval(plan)
    wall = time.monotonic() - started

    # Fast pool: 16 cases, cap 4 concurrent -> peak should be exactly 4.
    assert fast_adapter.peak_in_flight == 4, (
        f"fast pool peak {fast_adapter.peak_in_flight}, expected 4"
    )
    # Slow pool: 16 cases, cap 1 concurrent -> peak should be exactly 1.
    assert slow_adapter.peak_in_flight == 1, (
        f"slow pool peak {slow_adapter.peak_in_flight}, expected 1"
    )

    # Summary sanity: each variant completed all 16 cases.
    assert summary.variants[0].cases_total == 16
    assert summary.variants[1].cases_total == 16

    # Wall-time sanity: the slow pool serialises 16 * 0.05s = 0.8s
    # minimum; with concurrency=1 the slow variant dominates.
    assert wall >= 0.7, f"wall {wall:.2f}s too short — pools not enforced?"


async def test_pool_routing_falls_back_to_per_variant_when_unknown(
    tmp_path: Path,
) -> None:
    """A variant declaring `pool: X` where X isn't in `run.executor.pools`
    falls back to the per-variant default semaphore — failing closed
    would silently drop cells."""
    adapter = _ConcurrencyRecordingAdapter(sleep_s=0.01)
    cases = [EvalCase(id=f"c{i}", input={"q": i}) for i in range(8)]
    variants = [RunVariant(name="v1", adapter="fake", config={})]
    cfg = EvalConfig(
        eval=EvalIdentity(name="pools-fallback"),
        dataset=DatasetConfig(type="yaml", path="x"),
        systems=[SystemConfig(name="v1", adapter="fake", pool="nonexistent")],
        evaluators=[EvaluatorConfig(name="noop", type="noop")],
        pass_criteria=PassCriteria(),
        run=RunOptions(
            max_concurrency=4,
            retry=RetryPolicy(),
            executor=ExecutorConfig(type="local", pools={}),
        ),
        output=[OutputConfig(type="local_files", path="./runs")],
    )
    plan = RunPlan(
        config=cfg,
        run_id="r1",
        run_dir=tmp_path / "runs" / "r1",
        cases=cases,
        variants=variants,
        system_adapters={"v1": adapter},
        trace_store=_CapturingStore(),
        workspace=None,
        evaluators=[_NoopEvaluator(name="noop")],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
    )
    summary = await run_eval(plan)
    # No cap-4 pool; uses run.max_concurrency=4 via the per-variant semaphore.
    assert adapter.peak_in_flight == 4
    assert summary.variants[0].cases_total == 8


async def test_default_executor_used_when_run_executor_block_absent(
    tmp_path: Path,
) -> None:
    """No `run.executor` block -> LocalExecutor with no pools. Same
    behaviour as v1.x — backwards-compat guarantee."""
    adapter = _ConcurrencyRecordingAdapter(sleep_s=0.01)
    cases = [EvalCase(id=f"c{i}", input={"q": i}) for i in range(4)]
    cfg = EvalConfig(
        eval=EvalIdentity(name="default-executor"),
        dataset=DatasetConfig(type="yaml", path="x"),
        systems=[SystemConfig(name="v1", adapter="fake")],
        evaluators=[EvaluatorConfig(name="noop", type="noop")],
        pass_criteria=PassCriteria(),
        run=RunOptions(max_concurrency=2, retry=RetryPolicy()),
        output=[OutputConfig(type="local_files", path="./runs")],
    )
    plan = RunPlan(
        config=cfg,
        run_id="r1",
        run_dir=tmp_path / "runs" / "r1",
        cases=cases,
        variants=[RunVariant(name="v1", adapter="fake", config={})],
        system_adapters={"v1": adapter},
        trace_store=_CapturingStore(),
        workspace=None,
        evaluators=[_NoopEvaluator(name="noop")],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
    )
    summary = await run_eval(plan)
    # Default cfg.run.executor is ExecutorConfig(type="local", pools={}).
    # Peak in-flight respects max_concurrency=2.
    assert adapter.peak_in_flight == 2
    assert summary.cases_total == 4
