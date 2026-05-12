"""Runner integration for TraceEnricher: failure-soft + chained-in-order.

These tests mount fake adapters / evaluators on a hand-built RunPlan so the
runner's enrichment hook is exercised end-to-end without spinning up the
real config / factory layer. The fixture enricher is the seam — it records
what it saw and what it returned.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

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
    TraceOutput,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.base import Evaluator
from eval_harness.runner.plan_builder import RunPlan
from eval_harness.runner.run_eval import run_eval
from tests.fixtures.enrichers.fake_enricher import FakeEnricher

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


class _FakeStore:
    def __init__(self) -> None:
        self.traces: list[Trace] = []
        self.results: list[EvaluationResult] = []
        self.summary: RunSummary | None = None

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
        return None

    async def save_trace(self, trace: Trace) -> None:
        # Save a deep copy so the test observes the trace as persisted at
        # this point in the pipeline (post-enrichment).
        self.traces.append(trace.model_copy(deep=True))

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


class _ToyAdapter:
    def __init__(self) -> None:
        self.name = "toy"

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
            output=TraceOutput(final_answer=f"answer-{case.id}"),
        )


class _TraceCapturingEvaluator(Evaluator):
    """Evaluator that records the trace.extra it saw. The whole point of
    enrichment is that evaluators see the enriched trace."""

    type = "trace_capturing"

    def __init__(self, name: str, **_: Any) -> None:
        super().__init__(name=name)
        self.seen_extras: list[dict[str, Any]] = []

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        self.seen_extras.append(dict(trace.extra))
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=True,
            reason="captured",
            started_at=_NOW,
            finished_at=_NOW,
            latency_ms=0,
        )


def _config() -> EvalConfig:
    return EvalConfig(
        eval=EvalIdentity(name="enrichment-test"),
        dataset=DatasetConfig(type="yaml", path="x"),
        systems=[SystemConfig(name="v1", adapter="fake")],
        evaluators=[EvaluatorConfig(name="trace_eye", type="trace_capturing")],
        pass_criteria=PassCriteria(),
        run=RunOptions(max_concurrency=4, retry=RetryPolicy()),
        output=[OutputConfig(type="local_files", path="./runs")],
    )


def _make_plan(
    *,
    enrichers: list[FakeEnricher],
    evaluator: Evaluator,
    store: _FakeStore,
) -> tuple[RunPlan, _ToyAdapter]:
    adapter = _ToyAdapter()
    cfg = _config()
    variant = RunVariant(name="v1", adapter="fake", config={})
    plan = RunPlan(
        config=cfg,
        run_id="r1",
        run_dir=Path("./runs/r1"),
        cases=[EvalCase(id="c1", input={"q": "ping"})],
        variants=[variant],
        system_adapters={"v1": adapter},
        trace_store=store,
        workspace=None,
        evaluators=[evaluator],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
        enrichers={"v1": list(enrichers)},
    )
    return plan, adapter


# ---- Happy path ---------------------------------------------------------


async def test_enrich_appends_fields() -> None:
    """A single enricher contributing fields should land them on the trace
    that gets persisted and reaches the evaluator."""
    enricher = FakeEnricher(
        name="langfuse", enriched_fields={"langfuse_trace_id": "abc-123"}
    )
    store = _FakeStore()
    capture = _TraceCapturingEvaluator(name="cap")
    plan, _ = _make_plan(enrichers=[enricher], evaluator=capture, store=store)

    await run_eval(plan)

    assert enricher.call_count == 1
    assert enricher.entered and enricher.exited
    # The persisted trace carries the enrichment.
    assert store.traces[0].extra["enrichment"] == {"langfuse_trace_id": "abc-123"}
    # The evaluator saw the same enriched trace.
    assert capture.seen_extras[0]["enrichment"] == {"langfuse_trace_id": "abc-123"}


async def test_multiple_enrichers_chained_in_order() -> None:
    """A chain runs in config-declared order; each enricher sees the prior
    one's output. Evaluators see the merged result."""
    first = FakeEnricher(
        name="otel", enriched_fields={"otel_trace_id": "t1"}
    )
    second = FakeEnricher(
        name="langfuse", enriched_fields={"langfuse_trace_id": "L1"}
    )
    store = _FakeStore()
    capture = _TraceCapturingEvaluator(name="cap")
    plan, _ = _make_plan(
        enrichers=[first, second], evaluator=capture, store=store
    )

    await run_eval(plan)

    persisted = store.traces[0]
    assert persisted.extra["enriched_by"] == ["otel", "langfuse"]
    assert persisted.extra["enrichment"] == {
        "otel_trace_id": "t1",
        "langfuse_trace_id": "L1",
    }
    assert first.call_count == 1
    assert second.call_count == 1
    # Sanity: the evaluator's view matches the persisted view.
    assert capture.seen_extras[0]["enriched_by"] == ["otel", "langfuse"]


# ---- Failure-soft -------------------------------------------------------


async def test_enricher_failure_does_not_abort_cell() -> None:
    """The load-bearing invariant: an enricher that raises MUST NOT fail
    the cell. The runner records the failure on trace.extra and proceeds —
    the SystemAdapter still produces a trace, evaluators still run, the
    cell still appears in the summary."""
    failing = FakeEnricher(name="broken", should_raise=True)
    store = _FakeStore()
    capture = _TraceCapturingEvaluator(name="cap")
    plan, _ = _make_plan(enrichers=[failing], evaluator=capture, store=store)

    summary = await run_eval(plan)

    # Cell completed, ran the evaluator, surfaced in the summary.
    assert len(store.traces) == 1
    assert len(capture.seen_extras) == 1
    assert summary.variants[0].cases_passed == 1
    assert summary.variants[0].cases_errored == 0

    # The failure landed on trace.extra.enrichment_errors.
    errors = store.traces[0].extra.get("enrichment_errors")
    assert isinstance(errors, list) and len(errors) == 1
    assert errors[0]["enricher"] == "broken"
    assert "RuntimeError" in errors[0]["error"]


async def test_failing_enricher_does_not_block_later_enrichers() -> None:
    """If one enricher in a chain raises, subsequent enrichers still run
    against the trace that survived (the un-enriched one). The eval keeps
    the partial enrichment that DID land."""
    broken = FakeEnricher(name="broken", should_raise=True)
    healthy = FakeEnricher(
        name="healthy", enriched_fields={"healthy_marker": "yes"}
    )
    store = _FakeStore()
    capture = _TraceCapturingEvaluator(name="cap")
    plan, _ = _make_plan(
        enrichers=[broken, healthy], evaluator=capture, store=store
    )

    await run_eval(plan)

    persisted = store.traces[0]
    errors = persisted.extra.get("enrichment_errors") or []
    assert [e["enricher"] for e in errors] == ["broken"]
    assert persisted.extra.get("enrichment") == {"healthy_marker": "yes"}
    assert healthy.call_count == 1


async def test_no_enrichers_configured_is_a_noop() -> None:
    """The path without any enricher chain still saves an un-decorated trace."""
    store = _FakeStore()
    capture = _TraceCapturingEvaluator(name="cap")
    plan, _ = _make_plan(enrichers=[], evaluator=capture, store=store)

    await run_eval(plan)

    persisted = store.traces[0]
    assert "enriched_by" not in persisted.extra
    assert "enrichment" not in persisted.extra
    assert "enrichment_errors" not in persisted.extra


async def test_enricher_lifecycle_runs_once_per_run() -> None:
    """The runner enters each enricher's async context once and exits once,
    regardless of how many cells it serves."""
    enricher = FakeEnricher(name="single", enriched_fields={"k": "v"})
    store = _FakeStore()
    capture = _TraceCapturingEvaluator(name="cap")
    plan, _ = _make_plan(enrichers=[enricher], evaluator=capture, store=store)
    # Two cases, same variant -> enricher called twice but entered/exited once.
    plan.cases.append(EvalCase(id="c2", input={"q": "pong"}))

    await run_eval(plan)

    assert enricher.entered
    assert enricher.exited
    assert enricher.call_count == 2
