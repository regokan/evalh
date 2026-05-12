"""LocalExecutor — in-process implementation of the v2 Executor Protocol.

Wraps the existing async runner's `asyncio.gather` + `asyncio.Semaphore`
shape: there's no new dispatch overhead per cell beyond the cheap
`CellDescriptor` construction the runner already does. The cost-limit
guard, per-variant/global semaphores, streaming aggregator, cell-level
trace persistence, and AsyncExitStack-managed adapter lifecycle from
the v1.x runner are all preserved here.

Default executor: when `run.executor` is absent from the eval.yaml
the runner builds `LocalExecutor` — no breaking config change.

Capacity pools (v2 add): `run.executor.pools = {name: int}` declares
named pools; `systems[].pool` references one. Pool semaphores live on
the executor; absent pool routing falls back to the existing
per-variant / global semaphore.
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from eval_harness.core.executors.base import gather_outcomes
from eval_harness.core.models import (
    CellDescriptor,
    EvalCase,
    RunSummary,
    RunVariant,
)
from eval_harness.core.price_tables import warn_default_table_in_use
from eval_harness.runner.cost_accumulator import CostAccumulator
from eval_harness.runner.run_eval import (
    _build_variant_model_index,
    _enter_secondary,
    _fill_cost_from_price_table,
    _run_one,
    _secondary_call,
    _short_circuit_cost_limit,
    _variant_concurrency,
)
from eval_harness.runner.summary import SummaryAggregator

if TYPE_CHECKING:
    from eval_harness.runner.plan_builder import RunPlan
    from eval_harness.runner.run_eval import CellOutcome


class LocalExecutor:
    """In-process Executor. Owns per-run dispatch state.

    Lifecycle: ``async with executor; await executor.open(plan);
    executor.bind_cells(cells); handles = [submit_cell(c) for c]; await
    await_all(handles); summary = executor.finalize()``. The runner is
    responsible for calling ``save_summary`` on the canonical sink
    BEFORE leaving the ``async with`` block (the sink closes in
    ``__aexit__``).
    """

    def __init__(
        self,
        *,
        pools: dict[str, int] | None = None,
        **_extra: Any,
    ) -> None:
        self._pools_config: dict[str, int] = dict(pools or {})
        # Set in open(plan).
        self._plan: RunPlan | None = None
        self._accumulator: CostAccumulator | None = None
        self._aggregator: SummaryAggregator | None = None
        self._variant_models: dict[str, str] = {}
        self._sink_errors: list[dict[str, Any]] = []
        self._exit_stack: AsyncExitStack | None = None
        # Semaphore routing.
        self._pool_semaphores: dict[str, asyncio.Semaphore] = {}
        self._variant_semaphores: dict[str, asyncio.Semaphore] = {}
        # cell_id -> (case, variant). Bound by the runner via bind_cells()
        # so submit_cell doesn't pay pydantic re-validation per cell.
        self._cell_index: dict[str, tuple[EvalCase, RunVariant]] = {}

    # ---- Async context (lifecycle) -------------------------------------

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._exit_stack is not None:
            await self._exit_stack.aclose()
            self._exit_stack = None

    async def open(self, plan: RunPlan) -> None:
        """Build per-run state and enter the AsyncExitStack that owns
        the trace store, secondary stores, system adapters, enrichers,
        and workspace lifecycle. Called by the runner."""
        self._plan = plan
        self._accumulator = CostAccumulator()
        self._aggregator = SummaryAggregator(plan=plan)
        self._variant_models = _build_variant_model_index(plan.variants)
        self._build_semaphores(plan)

        if plan.price_table is not None:
            warn_default_table_in_use(plan.price_table)

        stack = AsyncExitStack()
        try:
            await stack.enter_async_context(plan.trace_store)
            for s in plan.secondary_trace_stores:
                await _enter_secondary(s, stack, self._sink_errors)
            for adapter in plan.system_adapters.values():
                await stack.enter_async_context(adapter)
            for chain in plan.enrichers.values():
                for enricher in chain:
                    await stack.enter_async_context(enricher)
            if plan.workspace is not None and hasattr(plan.workspace, "__aenter__"):
                await stack.enter_async_context(plan.workspace)  # type: ignore[arg-type]
            await plan.trace_store.open(plan.run_id, plan.run_dir)
            for s in plan.secondary_trace_stores:
                await _secondary_call(
                    s, "open", self._sink_errors, s.open, plan.run_id, plan.run_dir
                )
        except BaseException:
            await stack.aclose()
            raise
        self._exit_stack = stack

    def bind_cells(
        self,
        cells: list[tuple[CellDescriptor, EvalCase, RunVariant]],
    ) -> None:
        """Pre-bind the cell_id -> (case, variant) lookup so submit_cell
        is a dict lookup instead of pydantic re-validation. For the local
        path this keeps per-cell overhead at near-zero; distributed
        executors rehydrate case + variant from `cell.case_dict` /
        `cell.eval_config_dict` instead."""
        for cell, case, variant in cells:
            self._cell_index[cell.cell_id] = (case, variant)

    # ---- Submission / await --------------------------------------------

    async def submit_cell(self, cell: CellDescriptor) -> asyncio.Task[Any]:
        case, variant = self._cell_index[cell.cell_id]
        return asyncio.create_task(self._dispatch(cell, case, variant))

    async def dispatch_all(
        self, cells: list[CellDescriptor]
    ) -> list[CellOutcome]:
        """In-process bulk dispatch — matches the v1.x runner's exact
        ``asyncio.gather(*[run_cell(c, v) for c, v in cells])`` shape.

        The runner uses this as the single dispatch entry point, never
        branching on executor class. Distributed executors override
        with their own batched-RPC implementation."""
        cell_index = self._cell_index
        return await asyncio.gather(
            *(
                self._dispatch(cell, *cell_index[cell.cell_id])
                for cell in cells
            )
        )

    async def await_outcome(self, handle: asyncio.Task[Any]) -> Any:
        return await handle

    async def await_all(self, handles: list[asyncio.Task[Any]]) -> list[Any]:
        return await gather_outcomes(self, handles)

    async def close(self) -> None:
        # AsyncExitStack teardown lives in __aexit__ so it always runs,
        # even when the runner exits early via exception.
        return None

    # ---- Finalize -------------------------------------------------------

    def finalize(self) -> RunSummary:
        if self._aggregator is None:
            raise RuntimeError("LocalExecutor.finalize called before open()")
        summary = self._aggregator.finalize()
        summary.sink_errors = self._sink_errors
        return summary

    @property
    def sink_errors(self) -> list[dict[str, Any]]:
        return self._sink_errors

    # ---- Internals ------------------------------------------------------

    async def _dispatch(
        self, cell: CellDescriptor, case: EvalCase, variant: RunVariant
    ) -> CellOutcome:
        plan = self._plan
        accumulator = self._accumulator
        aggregator = self._aggregator
        assert plan is not None and accumulator is not None and aggregator is not None

        cost_limit = plan.config.run.cost_limit_usd
        semaphore = self._semaphore_for(cell, variant)
        async with semaphore:
            # Cost-guard inside the semaphore so still-queued cells
            # short-circuit instead of dispatching to the adapter.
            if accumulator.check_limit(cost_limit):
                outcome = await _short_circuit_cost_limit(
                    case,
                    variant,
                    plan,
                    accumulator.total_usd(),
                    cost_limit,
                    self._sink_errors,
                )
            else:
                outcome = await _run_one(case, variant, plan, self._sink_errors)
                _fill_cost_from_price_table(
                    outcome.trace,
                    self._variant_models.get(variant.name),
                    plan.price_table,
                )
                accumulator.tally(outcome.trace)
            aggregator.add(outcome)
        return outcome

    def _semaphore_for(
        self, cell: CellDescriptor, variant: RunVariant
    ) -> asyncio.Semaphore:
        # Pool routing wins; missing pool falls back to per-variant.
        if cell.pool is not None:
            pool_sem = self._pool_semaphores.get(cell.pool)
            if pool_sem is not None:
                return pool_sem
        return self._variant_semaphores[variant.name]

    def _build_semaphores(self, plan: RunPlan) -> None:
        for name, cap in self._pools_config.items():
            self._pool_semaphores[name] = asyncio.Semaphore(cap)

        global_cap = plan.config.run.max_concurrency
        per_variant_cap = plan.config.run.per_variant_concurrency
        any_override = any(_variant_concurrency(v) is not None for v in plan.variants)
        if not any_override and per_variant_cap is None:
            shared = asyncio.Semaphore(global_cap)
            for v in plan.variants:
                self._variant_semaphores[v.name] = shared
            return
        default = per_variant_cap or global_cap
        for v in plan.variants:
            cap = _variant_concurrency(v) or default
            self._variant_semaphores[v.name] = asyncio.Semaphore(cap)


def _attach_normalizers() -> None:
    """Late import + re-export of helper for tests that want to build
    LocalExecutor directly without dragging in the runner module."""
    return None


# Re-export the helpers used by submit_cell so the tests have a stable
# import surface that doesn't depend on the runner module's internals.
__all__ = [
    "LocalExecutor",
]
