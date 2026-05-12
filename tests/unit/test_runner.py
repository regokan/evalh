from __future__ import annotations

import asyncio
import time
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest

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
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    RunVariant,
    Trace,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.base import Evaluator
from eval_harness.runner.plan_builder import RunPlan
from eval_harness.runner.run_eval import run_eval


class FakeTraceStore:
    def __init__(self) -> None:
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


class SleepingAdapter:
    def __init__(self, name: str, sleep_seconds: float = 0.0) -> None:
        self.name = name
        self.sleep_seconds = sleep_seconds
        self.in_flight = 0
        self.peak_in_flight = 0

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
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Any,
    ) -> Trace:
        self.in_flight += 1
        self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
        try:
            if self.sleep_seconds > 0:
                await asyncio.sleep(self.sleep_seconds)
        finally:
            self.in_flight -= 1
        # Adapter lies about latency — runner must overwrite.
        now = utc_now()
        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=now,
            finished_at=now,
            latency_ms=99999,
            input=dict(case.input),
            output=TraceOutput(final_answer=f"answer for {case.id}@{variant.name}"),
            metrics=TraceMetrics(token_input=10, token_output=20, cost_usd=0.001),
        )


class HangingAdapter:
    def __init__(self, name: str) -> None:
        self.name = name

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self, exc_type: object, exc: object, tb: object
    ) -> None:
        return None

    async def run(self, case: EvalCase, variant: RunVariant, workspace: Any) -> Trace:
        await asyncio.sleep(10.0)
        raise AssertionError("should have timed out")


class PassingEvaluator(Evaluator):
    type = "passing"

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


class CrashingEvaluator(Evaluator):
    type = "crashing"

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        raise RuntimeError("boom")


class FailingForVariantEvaluator(Evaluator):
    """Passes only for the configured variant_name; fails for others.

    Used to construct a per-variant pass/fail pattern for baseline-comparison tests.
    """

    type = "variant_gated"

    def __init__(self, name: str, *, pass_variant: str, **_: Any) -> None:
        super().__init__(name=name)
        self._pass_variant = pass_variant

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        now = utc_now()
        passed = trace.variant_name == self._pass_variant
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=passed,
            reason="match" if passed else "wrong variant",
            started_at=now,
            finished_at=now,
            latency_ms=0,
        )


def _make_cases(n: int) -> list[EvalCase]:
    return [EvalCase(id=f"case_{i}", input={"q": i}) for i in range(n)]


def _make_variant(name: str, *, concurrency: int | None = None, timeout: float | None = None) -> RunVariant:
    config: dict[str, Any] = {}
    if concurrency is not None:
        config["concurrency"] = concurrency
    if timeout is not None:
        config["timeout_seconds"] = timeout
    return RunVariant(name=name, adapter="fake", config=config)


def _make_config(
    *,
    max_concurrency: int = 4,
    pass_criteria: PassCriteria | None = None,
    baseline: str | None = None,
    systems: list[SystemConfig] | None = None,
) -> EvalConfig:
    return EvalConfig(
        eval=EvalIdentity(name="t"),
        dataset=DatasetConfig(type="yaml", path="x"),
        systems=systems or [SystemConfig(name="v", adapter="fake")],
        evaluators=[EvaluatorConfig(name="ev", type="passing")],
        pass_criteria=pass_criteria or PassCriteria(),
        run=RunOptions(
            max_concurrency=max_concurrency,
            retry=RetryPolicy(),
            baseline_variant=baseline,
        ),
        output=[OutputConfig(type="local_files", path="./runs")],
    )


def _make_plan(
    *,
    cases: list[EvalCase],
    variants: list[RunVariant],
    adapters: dict[str, Any],
    evaluators: list[Evaluator],
    store: FakeTraceStore,
    config: EvalConfig | None = None,
    baseline: str | None = None,
) -> RunPlan:
    cfg = config or _make_config(baseline=baseline)
    return RunPlan(
        config=cfg,
        run_id="test-run",
        run_dir=Path("./runs/test-run"),
        cases=cases,
        variants=variants,
        system_adapters=adapters,
        trace_store=store,
        workspace=None,
        evaluators=evaluators,
        retry_policy=cfg.run.retry,
        baseline_variant=baseline,
    )


async def test_two_cells_two_variants_run_in_parallel() -> None:
    sleep_s = 0.2
    a = SleepingAdapter("v1", sleep_seconds=sleep_s)
    b = SleepingAdapter("v2", sleep_seconds=sleep_s)
    cases = _make_cases(2)
    variants = [_make_variant("v1"), _make_variant("v2")]
    store = FakeTraceStore()
    plan = _make_plan(
        cases=cases,
        variants=variants,
        adapters={"v1": a, "v2": b},
        evaluators=[PassingEvaluator(name="ev")],
        store=store,
        config=_make_config(max_concurrency=4),
    )

    started = time.perf_counter()
    summary = await run_eval(plan)
    elapsed = time.perf_counter() - started

    # 4 cells * 0.2s serialized would be 0.8s; with concurrency=4 it should be ~0.2s.
    assert elapsed < 0.6
    assert len(store.traces) == 4
    assert summary.cases_total == 2
    assert all(vs.cases_passed == 2 for vs in summary.variants)


async def test_one_cell_timeout_records_error_in_trace() -> None:
    adapter = HangingAdapter("v1")
    cases = _make_cases(1)
    variants = [_make_variant("v1", timeout=0.05)]
    store = FakeTraceStore()
    plan = _make_plan(
        cases=cases,
        variants=variants,
        adapters={"v1": adapter},
        evaluators=[PassingEvaluator(name="ev")],
        store=store,
    )

    summary = await run_eval(plan)

    assert len(store.traces) == 1
    trace = store.traces[0]
    assert trace.error is not None
    assert trace.error.type == "timeout"
    assert summary.variants[0].cases_errored == 1


async def test_one_evaluator_exception_isolated_other_results_present() -> None:
    adapter = SleepingAdapter("v1")
    cases = _make_cases(1)
    variants = [_make_variant("v1")]
    store = FakeTraceStore()
    evaluators: list[Evaluator] = [
        CrashingEvaluator(name="crash"),
        PassingEvaluator(name="ok"),
    ]
    plan = _make_plan(
        cases=cases,
        variants=variants,
        adapters={"v1": adapter},
        evaluators=evaluators,
        store=store,
    )

    await run_eval(plan)

    assert len(store.results) == 2
    by_name = {r.evaluator: r for r in store.results}
    assert by_name["crash"].passed is False
    assert by_name["crash"].error is not None
    assert by_name["ok"].passed is True


async def test_concurrency_bounded_by_semaphore() -> None:
    adapter = SleepingAdapter("v1", sleep_seconds=0.05)
    cases = _make_cases(10)
    variants = [_make_variant("v1")]
    store = FakeTraceStore()
    plan = _make_plan(
        cases=cases,
        variants=variants,
        adapters={"v1": adapter},
        evaluators=[PassingEvaluator(name="ev")],
        store=store,
        config=_make_config(max_concurrency=3),
    )

    await run_eval(plan)

    assert adapter.peak_in_flight <= 3
    assert len(store.traces) == 10


async def test_baseline_comparison_emits_deltas() -> None:
    a = SleepingAdapter("baseline")
    b = SleepingAdapter("candidate")
    cases = _make_cases(2)
    variants = [_make_variant("baseline"), _make_variant("candidate")]
    store = FakeTraceStore()
    evaluators: list[Evaluator] = [
        FailingForVariantEvaluator(name="gate", pass_variant="baseline"),
    ]
    config = _make_config(
        baseline="baseline",
        systems=[
            SystemConfig(name="baseline", adapter="fake"),
            SystemConfig(name="candidate", adapter="fake"),
        ],
    )
    plan = _make_plan(
        cases=cases,
        variants=variants,
        adapters={"baseline": a, "candidate": b},
        evaluators=evaluators,
        store=store,
        config=config,
        baseline="baseline",
    )

    summary = await run_eval(plan)

    assert summary.comparison is not None
    assert summary.comparison.baseline == "baseline"
    assert len(summary.comparison.deltas) == 1
    delta = summary.comparison.deltas[0]
    assert delta.variant == "candidate"
    # Baseline passed all; candidate failed all -> regressions cover every case id.
    assert sorted(delta.regressions) == ["case_0", "case_1"]
    assert delta.improvements == []
    assert delta.pass_rate_delta == pytest.approx(-1.0)


async def test_latency_invariant_overwrites_adapter_value() -> None:
    adapter = SleepingAdapter("v1", sleep_seconds=0.0)
    cases = _make_cases(1)
    variants = [_make_variant("v1")]
    store = FakeTraceStore()
    plan = _make_plan(
        cases=cases,
        variants=variants,
        adapters={"v1": adapter},
        evaluators=[PassingEvaluator(name="ev")],
        store=store,
    )

    await run_eval(plan)

    trace = store.traces[0]
    assert trace.latency_ms != 99999  # adapter lied; runner overwrote
    assert trace.latency_ms >= 0
    assert isinstance(trace.started_at, datetime)
    assert trace.finished_at >= trace.started_at


class TokenOnlyAdapter:
    """Adapter that reports tokens but not cost — exercises price-table fill."""

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
        self, case: EvalCase, variant: RunVariant, workspace: Any
    ) -> Trace:
        now = utc_now()
        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=now,
            finished_at=now,
            latency_ms=1,
            input=dict(case.input),
            output=TraceOutput(final_answer="x"),
            metrics=TraceMetrics(
                token_input=1_000_000, token_output=500_000, cost_usd=None
            ),
        )


async def test_runner_fills_cost_from_price_table_when_adapter_omits_it() -> None:
    from eval_harness.core.price_tables import DEFAULT_PRICE_TABLE

    adapter = TokenOnlyAdapter("v1")
    cases = _make_cases(1)
    variants = [
        RunVariant(name="v1", adapter="fake", config={}, metadata={"model": "claude-4-7"})
    ]
    store = FakeTraceStore()
    plan = _make_plan(
        cases=cases,
        variants=variants,
        adapters={"v1": adapter},
        evaluators=[PassingEvaluator(name="ev")],
        store=store,
    )
    plan.price_table = DEFAULT_PRICE_TABLE

    await run_eval(plan)

    trace = store.traces[0]
    # claude-4-7: $3/M input + $15/M output -> 1M*$3 + 0.5M*$15 = $10.5
    assert trace.metrics.cost_usd == pytest.approx(10.5)


async def test_runner_leaves_cost_alone_when_adapter_reported_it() -> None:
    from eval_harness.core.price_tables import DEFAULT_PRICE_TABLE

    adapter = SleepingAdapter("v1")  # reports cost_usd=0.001
    cases = _make_cases(1)
    variants = [
        RunVariant(name="v1", adapter="fake", config={}, metadata={"model": "claude-4-7"})
    ]
    store = FakeTraceStore()
    plan = _make_plan(
        cases=cases,
        variants=variants,
        adapters={"v1": adapter},
        evaluators=[PassingEvaluator(name="ev")],
        store=store,
    )
    plan.price_table = DEFAULT_PRICE_TABLE

    await run_eval(plan)

    trace = store.traces[0]
    assert trace.metrics.cost_usd == pytest.approx(0.001)


async def test_runner_skips_fill_for_unknown_model() -> None:
    from eval_harness.core.price_tables import DEFAULT_PRICE_TABLE

    adapter = TokenOnlyAdapter("v1")
    cases = _make_cases(1)
    variants = [
        RunVariant(
            name="v1", adapter="fake", config={}, metadata={"model": "made-up-model"}
        )
    ]
    store = FakeTraceStore()
    plan = _make_plan(
        cases=cases,
        variants=variants,
        adapters={"v1": adapter},
        evaluators=[PassingEvaluator(name="ev")],
        store=store,
    )
    plan.price_table = DEFAULT_PRICE_TABLE

    await run_eval(plan)

    trace = store.traces[0]
    assert trace.metrics.cost_usd is None  # unknown model, runner left it
