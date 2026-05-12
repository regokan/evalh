from __future__ import annotations

import asyncio
import itertools
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING

from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    RunSummary,
    RunVariant,
    Trace,
    TraceError,
)
from eval_harness.core.time import utc_now
from eval_harness.runner.cost_accumulator import CostAccumulator
from eval_harness.runner.summary import build_summary

if TYPE_CHECKING:
    from eval_harness.adapters.workspace.base import Workspace
    from eval_harness.evaluators.base import Evaluator
    from eval_harness.runner.plan_builder import RunPlan


@dataclass
class CellOutcome:
    case: EvalCase
    variant: RunVariant
    trace: Trace
    results: list[EvaluationResult]


async def run_eval(plan: RunPlan) -> RunSummary:
    semaphores = _build_semaphores(plan)
    accumulator = CostAccumulator()
    cost_limit = plan.config.run.cost_limit_usd

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(plan.trace_store)
        for adapter in plan.system_adapters.values():
            await stack.enter_async_context(adapter)
        if plan.workspace is not None and hasattr(plan.workspace, "__aenter__"):
            await stack.enter_async_context(plan.workspace)  # type: ignore[arg-type]

        await plan.trace_store.open(plan.run_id, plan.run_dir)

        cells = list(itertools.product(plan.cases, plan.variants))
        if plan.cell_filter is not None:
            cells = [
                (c, v) for c, v in cells if (c.id, v.name) in plan.cell_filter
            ]

        async def run_cell(case: EvalCase, variant: RunVariant) -> CellOutcome:
            async with semaphores[variant.name]:
                # Soft guardrail: check inside the semaphore so still-queued
                # cells short-circuit instead of dispatching to the adapter.
                # In-flight cells (already past this check) finish naturally.
                if accumulator.check_limit(cost_limit):
                    return _cost_limit_outcome(
                        case, variant, plan.run_id, accumulator.total_usd(), cost_limit
                    )
                outcome = await _run_one(case, variant, plan)
                accumulator.tally(outcome.trace)
                return outcome

        outcomes = await asyncio.gather(
            *[run_cell(c, v) for c, v in cells],
            return_exceptions=False,
        )

        # Persist short-circuited traces so summary.yaml + traces.jsonl are
        # consistent with what the runner returns. _run_one already saved
        # the others.
        for outcome in outcomes:
            if outcome.trace.error is not None and outcome.trace.error.type == "cost_limit":
                await plan.trace_store.save_trace(outcome.trace)

        summary = build_summary(outcomes, plan)
        await plan.trace_store.save_summary(summary)
        return summary

    raise RuntimeError("unreachable: AsyncExitStack never re-raises")


def _cost_limit_outcome(
    case: EvalCase,
    variant: RunVariant,
    run_id: str,
    accumulated: float,
    limit: float | None,
) -> CellOutcome:
    now = utc_now()
    trace = Trace.from_error(
        case.id,
        variant.name,
        "cost_limit",
        (
            f"cost limit ${limit:.4f} exceeded, accumulated ${accumulated:.4f}"
            if limit is not None
            else f"cost limit exceeded, accumulated ${accumulated:.4f}"
        ),
    )
    trace.run_id = run_id
    trace.started_at = now
    trace.finished_at = now
    trace.latency_ms = 0
    return CellOutcome(case=case, variant=variant, trace=trace, results=[])


def _build_semaphores(plan: RunPlan) -> dict[str, asyncio.Semaphore]:
    global_cap = plan.config.run.max_concurrency
    per_variant: dict[str, asyncio.Semaphore] = {}
    any_override = any(_variant_concurrency(v) is not None for v in plan.variants)
    if not any_override and plan.config.run.per_variant_concurrency is None:
        shared = asyncio.Semaphore(global_cap)
        return {v.name: shared for v in plan.variants}

    default = plan.config.run.per_variant_concurrency or global_cap
    for v in plan.variants:
        cap = _variant_concurrency(v) or default
        per_variant[v.name] = asyncio.Semaphore(cap)
    return per_variant


def _variant_concurrency(variant: RunVariant) -> int | None:
    raw = variant.config.get("concurrency")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    return None


async def _run_one(case: EvalCase, variant: RunVariant, plan: RunPlan) -> CellOutcome:
    workspace: Workspace | None = None
    try:
        if plan.workspace is not None:
            workspace = await plan.workspace.prepare(case, variant)

        timeout = _timeout_for(variant, plan)
        started_at = utc_now()
        adapter = plan.system_adapters[variant.name]
        try:
            trace = await asyncio.wait_for(
                adapter.run(case, variant, workspace), timeout=timeout
            )
        except TimeoutError as exc:
            trace = Trace.from_error(case.id, variant.name, "timeout", str(exc) or "timed out")
        except Exception as exc:
            trace = Trace.from_error(
                case.id, variant.name, "adapter_error", f"{type(exc).__name__}: {exc}"
            )
        finished_at = utc_now()

        _enforce_invariants(trace, plan.run_id, case, variant, started_at, finished_at)

        await plan.trace_store.save_trace(trace)

        artifact = None
        if workspace is not None and plan.workspace is not None:
            artifact = await plan.workspace.collect_artifacts(workspace)
            await plan.trace_store.save_artifact(artifact)

        raw_results = await asyncio.gather(
            *[ev.evaluate(case, trace, artifact) for ev in plan.evaluators],
            return_exceptions=True,
        )
        results = [
            _normalize_result(r, ev, plan.run_id, case, variant)
            for r, ev in zip(raw_results, plan.evaluators, strict=True)
        ]
        await plan.trace_store.save_evaluation(case.id, variant.name, results)

        return CellOutcome(case=case, variant=variant, trace=trace, results=results)

    finally:
        if workspace is not None and plan.workspace is not None:
            with suppress(Exception):
                await plan.workspace.cleanup(workspace)


def _timeout_for(variant: RunVariant, plan: RunPlan) -> float:
    raw = variant.config.get("timeout_seconds")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    return 120.0


def _enforce_invariants(
    trace: Trace,
    run_id: str,
    case: EvalCase,
    variant: RunVariant,
    started_at: object,
    finished_at: object,
) -> None:
    trace.run_id = run_id
    trace.case_id = case.id
    trace.variant_name = variant.name
    # Latency invariant: the runner is the source of truth, not the adapter.
    trace.started_at = started_at  # type: ignore[assignment]
    trace.finished_at = finished_at  # type: ignore[assignment]
    delta = (finished_at - started_at).total_seconds()  # type: ignore[operator]
    trace.latency_ms = max(0, int(delta * 1000))


def _normalize_result(
    raw: object,
    evaluator: Evaluator,
    run_id: str,
    case: EvalCase,
    variant: RunVariant,
) -> EvaluationResult:
    if isinstance(raw, EvaluationResult):
        raw.run_id = run_id
        raw.case_id = case.id
        raw.variant_name = variant.name
        return raw
    if isinstance(raw, BaseException):
        now = utc_now()
        return EvaluationResult(
            run_id=run_id,
            case_id=case.id,
            variant_name=variant.name,
            evaluator=evaluator.name,
            evaluator_type=evaluator.type,
            passed=False,
            reason=f"evaluator '{evaluator.name}' crashed: {raw}",
            started_at=now,
            finished_at=now,
            latency_ms=0,
            error=TraceError(type=type(raw).__name__, message=str(raw)),
        )
    raise TypeError(f"evaluator '{evaluator.name}' returned non-EvaluationResult: {type(raw).__name__}")
