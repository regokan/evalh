from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    RunSummary,
    RunVariant,
    Trace,
    TraceError,
)
from eval_harness.core.price_tables import (
    PriceTable,
    compute_cost,
    warn_default_table_in_use,
)
from eval_harness.core.time import utc_now
from eval_harness.runner.cost_accumulator import CostAccumulator
from eval_harness.runner.summary import SummaryAggregator

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from eval_harness.adapters.enricher.base import TraceEnricher
    from eval_harness.adapters.trace.base import TraceStore
    from eval_harness.adapters.workspace.base import Workspace
    from eval_harness.evaluators.base import Evaluator
    from eval_harness.runner.plan_builder import RunPlan


def _sink_name(store: object) -> str:
    return type(store).__name__


async def _secondary_call(
    store: TraceStore,
    op: str,
    sink_errors: list[dict[str, Any]],
    fn: Callable[..., Awaitable[None]],
    /,
    *args: Any,
    **kwargs: Any,
) -> None:
    """Invoke a save / open on a non-canonical sink; trap any exception into
    `sink_errors`. Never re-raises — secondary failures must not abort the
    run."""
    try:
        await fn(*args, **kwargs)
    except Exception as exc:
        sink_errors.append(
            {
                "sink": _sink_name(store),
                "op": op,
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        logger.warning(
            "multi-sink: secondary store %s failed on %s: %s",
            _sink_name(store),
            op,
            exc,
        )


async def _enter_secondary(
    store: TraceStore,
    stack: AsyncExitStack,
    sink_errors: list[dict[str, Any]],
) -> None:
    """Best-effort __aenter__ for a secondary sink. Failure is logged into
    sink_errors and the sink is removed from the dispatch list (the caller
    keeps using the original list, so we instead just let later save_* calls
    fail and record those — simpler and still correct: subsequent ops on a
    sink whose enter failed will themselves log)."""
    try:
        await stack.enter_async_context(store)
    except Exception as exc:
        sink_errors.append(
            {
                "sink": _sink_name(store),
                "op": "open",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        logger.warning(
            "multi-sink: secondary store %s failed on enter: %s",
            _sink_name(store),
            exc,
        )


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
    aggregator = SummaryAggregator(plan=plan)
    if plan.price_table is not None:
        warn_default_table_in_use(plan.price_table)
    variant_models = _build_variant_model_index(plan.variants)
    # Multi-sink output: collected best-effort failures land on
    # RunSummary.sink_errors at finalize time. See docs/Observability.md.
    sink_errors: list[dict[str, Any]] = []

    async with AsyncExitStack() as stack:
        await stack.enter_async_context(plan.trace_store)
        for s in plan.secondary_trace_stores:
            await _enter_secondary(s, stack, sink_errors)
        for adapter in plan.system_adapters.values():
            await stack.enter_async_context(adapter)
        for chain in plan.enrichers.values():
            for enricher in chain:
                await stack.enter_async_context(enricher)
        if plan.workspace is not None and hasattr(plan.workspace, "__aenter__"):
            await stack.enter_async_context(plan.workspace)  # type: ignore[arg-type]

        await plan.trace_store.open(plan.run_id, plan.run_dir)
        for s in plan.secondary_trace_stores:
            await _secondary_call(s, "open", sink_errors, s.open, plan.run_id, plan.run_dir)

        cells = list(itertools.product(plan.cases, plan.variants))
        if plan.cell_filter is not None:
            cells = [
                (c, v) for c, v in cells if (c.id, v.name) in plan.cell_filter
            ]

        async def run_cell(case: EvalCase, variant: RunVariant) -> None:
            async with semaphores[variant.name]:
                # Soft guardrail: check inside the semaphore so still-queued
                # cells short-circuit instead of dispatching to the adapter.
                # In-flight cells (already past this check) finish naturally.
                if accumulator.check_limit(cost_limit):
                    outcome = await _short_circuit_cost_limit(
                        case,
                        variant,
                        plan,
                        accumulator.total_usd(),
                        cost_limit,
                        sink_errors,
                    )
                else:
                    outcome = await _run_one(case, variant, plan, sink_errors)
                    _fill_cost_from_price_table(
                        outcome.trace, variant_models.get(variant.name), plan.price_table
                    )
                    accumulator.tally(outcome.trace)
                # Stream the outcome into the aggregator immediately and let
                # it fall out of scope. This is the streaming-summary
                # discipline: the runner's heap stays O(in-flight cells),
                # never O(total cases).
                aggregator.add(outcome)

        await asyncio.gather(
            *[run_cell(c, v) for c, v in cells],
            return_exceptions=False,
        )

        summary = aggregator.finalize()
        # Share the same list — later best-effort save_summary failures on
        # mirrors append into it and the returned RunSummary reflects them.
        # (The persisted summary.yaml on the canonical sink necessarily
        # predates save_summary-stage mirror failures; that asymmetry is
        # acceptable per docs/Observability.md — local files is canonical.)
        summary.sink_errors = sink_errors
        # Canonical save first; if it fails the run is meaningless.
        await plan.trace_store.save_summary(summary)
        for s in plan.secondary_trace_stores:
            await _secondary_call(
                s, "save_summary", sink_errors, s.save_summary, summary
            )
        return summary

    raise RuntimeError("unreachable: AsyncExitStack never re-raises")


async def _run_enrichers(
    trace: Trace,
    enrichers: list[TraceEnricher],
) -> Trace:
    """Dispatch the variant's enricher chain. Failure-soft: an enricher that
    raises does NOT abort the cell — the runner records the failure on
    ``trace.extra.enrichment_errors`` and proceeds with the un-enriched
    trace from that point in the chain. See
    ``docs/Adapters.md`` > TraceEnricher."""
    for enricher in enrichers:
        try:
            trace = await enricher.enrich(trace)
        except Exception as exc:
            errors = trace.extra.setdefault("enrichment_errors", [])
            errors.append(
                {
                    "enricher": getattr(enricher, "name", type(enricher).__name__),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            logger.debug(
                "enricher %r failed (failure-soft): %s",
                getattr(enricher, "name", type(enricher).__name__),
                exc,
            )
    return trace


def _build_variant_model_index(variants: list[RunVariant]) -> dict[str, str]:
    """Map variant name -> declared model. Looks in `metadata.model` first
    (the canonical place per docs/ConfigSchema.md), then `config.model` for
    adapters that nest it there. Variants without a declared model are
    omitted; the runner just doesn't fill cost for them."""
    out: dict[str, str] = {}
    for v in variants:
        raw = v.metadata.get("model") or v.config.get("model")
        if isinstance(raw, str) and raw:
            out[v.name] = raw
    return out


def _fill_cost_from_price_table(
    trace: Trace, model: str | None, table: PriceTable | None
) -> None:
    """If the adapter didn't fill `cost_usd` but did report token counts, use
    the price table. No-op when prices are unavailable for the model."""
    if table is None or model is None:
        return
    metrics = trace.metrics
    if metrics.cost_usd is not None:
        return
    token_input = metrics.token_input or 0
    token_output = metrics.token_output or 0
    token_thinking = metrics.token_thinking or 0
    if token_input == 0 and token_output == 0 and token_thinking == 0:
        return
    cost = compute_cost(table, model, token_input, token_output, token_thinking)
    if cost is None:
        logger.debug(
            "price_table: no entry for model %r (variant %r); cost_usd left None",
            model,
            trace.variant_name,
        )
        return
    metrics.cost_usd = cost
    logger.debug(
        "price_table: filled cost_usd=%.6f for variant=%r model=%r "
        "(in=%d, out=%d, thinking=%d)",
        cost,
        trace.variant_name,
        model,
        token_input,
        token_output,
        token_thinking,
    )


async def _short_circuit_cost_limit(
    case: EvalCase,
    variant: RunVariant,
    plan: RunPlan,
    accumulated: float,
    limit: float | None,
    sink_errors: list[dict[str, Any]],
) -> CellOutcome:
    """Build, persist, and return a cost_limit cell outcome.

    Owning the persist side here keeps the runner's main loop ignorant of
    error categories — per ``.claude/rules/architecture.md`` (the
    runner-stays-boring rule), branching on ``trace.error.type`` belongs in
    a helper, not in ``run_cell``.
    """
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
    trace.run_id = plan.run_id
    trace.started_at = now
    trace.finished_at = now
    trace.latency_ms = 0
    await plan.trace_store.save_trace(trace)
    for s in plan.secondary_trace_stores:
        await _secondary_call(s, "save_trace", sink_errors, s.save_trace, trace)
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


async def _run_one(
    case: EvalCase,
    variant: RunVariant,
    plan: RunPlan,
    sink_errors: list[dict[str, Any]],
) -> CellOutcome:
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

        trace = await _run_enrichers(trace, plan.enrichers.get(variant.name, []))

        await plan.trace_store.save_trace(trace)
        for s in plan.secondary_trace_stores:
            await _secondary_call(s, "save_trace", sink_errors, s.save_trace, trace)

        artifact = None
        if workspace is not None and plan.workspace is not None:
            artifact = await plan.workspace.collect_artifacts(workspace)
            await plan.trace_store.save_artifact(artifact)
            for s in plan.secondary_trace_stores:
                await _secondary_call(
                    s, "save_artifact", sink_errors, s.save_artifact, artifact
                )

        raw_results = await asyncio.gather(
            *[ev.evaluate(case, trace, artifact) for ev in plan.evaluators],
            return_exceptions=True,
        )
        results = [
            _normalize_result(r, ev, plan.run_id, case, variant)
            for r, ev in zip(raw_results, plan.evaluators, strict=True)
        ]
        await plan.trace_store.save_evaluation(case.id, variant.name, results)
        for s in plan.secondary_trace_stores:
            await _secondary_call(
                s,
                "save_evaluation",
                sink_errors,
                s.save_evaluation,
                case.id,
                variant.name,
                results,
            )

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
    # Replay traces preserve the ORIGINAL upstream timestamps, latency, and
    # metrics byte-for-byte — the trace describes what happened, not what the
    # harness did with it. The runner must not overwrite. See
    # docs/Observability.md > Pattern 4. The opt-out is explicit (the runner
    # checks ``extra.source``) rather than implicit (a flag the adapter sets)
    # so future debuggers can grep for it.
    if trace.extra.get("source") == "replay":
        return
    # Latency invariant: for everything else the runner is the source of
    # truth, not the adapter.
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
