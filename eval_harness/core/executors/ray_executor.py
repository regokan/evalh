"""RayExecutor — Ray tasks as the dispatch primitive.

Each cell becomes a ``ray.remote`` task. The orchestrator submits a
serialised ``CellDescriptor`` payload (with ``case_dict`` /
``eval_config_dict`` populated); the Ray worker calls
``_worker.worker_run_cell_sync``, which rebuilds adapters + evaluators
from entry-points and returns an outcome dict.

Ray ships an in-process mode (``ray.init()`` with no cluster address)
that's enough for the integration test. Real cluster runs are driven by
the manual benchmark (``benchmarks/distributed_1m.py`` per VAL).

See docs/Executors.md for the deployment recipe.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from contextlib import AsyncExitStack
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast

from eval_harness.core.errors import ConfigError
from eval_harness.core.executors._worker import worker_run_cell_sync
from eval_harness.core.executors.base import (
    gather_outcomes,
    warn_if_local_files_with_distributed,
)
from eval_harness.core.models import (
    CellDescriptor,
    EvalCase,
    EvaluationResult,
    RunSummary,
    RunVariant,
    Trace,
)
from eval_harness.core.price_tables import warn_default_table_in_use
from eval_harness.runner.cost_accumulator import CostAccumulator
from eval_harness.runner.summary import SummaryAggregator

if TYPE_CHECKING:
    from eval_harness.runner.plan_builder import RunPlan
    from eval_harness.runner.run_eval import CellOutcome


class RayExecutor:
    """Dispatches each cell as a Ray task.

    Config:
    - ``address`` (str, default ``"auto"``): cluster address forwarded to
      ``ray.init``. ``"auto"`` connects to a running cluster if any, else
      starts a local one. Tests use ``None`` (or omit) to force an
      in-process cluster.
    - ``runtime_env`` (dict, optional): forwarded to ``ray.init`` — the
      most common knob is ``{"pip": ["eval-harness", "my-plugin"]}`` so
      Ray workers install the same plugin set the orchestrator has.
      Like Modal's image_spec, this is the channel for the
      entry-point-set-must-match contract.
    - ``num_cpus_per_cell`` (int, default 1): per-task resource request.
    - ``num_gpus_per_cell`` (float | None): forwarded to ``ray.remote``
      when set.
    - ``object_store_memory`` (int, default 78 643 200 ≈ 75 MiB):
      forwarded to ``ray.init``. The default is sized for GitHub Actions
      runners whose ``/dev/shm`` defaults to ~64 MiB; Ray's normal
      auto-sizing tries to grab 2 GiB of plasma store on Linux and
      crashes ``ray.init`` on a tiny ``/dev/shm``. Real-cluster runs
      should override this with a production value (Ray's normal
      default is ``None`` = auto-size); the executor is faithful about
      forwarding whatever the caller sets.
    - ``ray_module`` (Any, test seam): a stand-in for the real ``ray``
      module. Production callers leave this ``None``; tests inject a
      fake exposing ``init``, ``shutdown``, ``remote``, and ``get``.
    """

    def __init__(
        self,
        *,
        address: str | None = "auto",
        runtime_env: dict[str, Any] | None = None,
        num_cpus_per_cell: int = 1,
        num_gpus_per_cell: float | None = None,
        object_store_memory: int | None = 78_643_200,
        ray_module: Any | None = None,
        **_extra: Any,
    ) -> None:
        if ray_module is None:
            try:
                import ray as _ray
            except ImportError as e:
                raise ConfigError(
                    "ray executor requires the `ray` package. Install with: "
                    "pip install 'eval-harness[ray]'"
                ) from e
            ray_module = _ray

        self._ray: Any = ray_module
        self._address = address
        self._runtime_env = dict(runtime_env) if runtime_env else None
        self._num_cpus_per_cell = int(num_cpus_per_cell)
        self._num_gpus_per_cell = num_gpus_per_cell
        self._object_store_memory = object_store_memory

        # Per-run state.
        self._plan: RunPlan | None = None
        self._accumulator: CostAccumulator | None = None
        self._aggregator: SummaryAggregator | None = None
        self._sink_errors: list[dict[str, Any]] = []
        self._cell_index: dict[str, tuple[EvalCase, RunVariant]] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._remote_fn: Any | None = None
        self._we_initialised_ray = False

    # ---- lifecycle -----------------------------------------------------

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
        # Only shut down Ray if we started it — callers who pre-initialised
        # Ray themselves (e.g. multi-run scripts) keep their cluster alive.
        if self._we_initialised_ray:
            # Shutdown is best-effort: if the cluster is already torn down
            # by a co-tenant, propagating that error would mask the real
            # exit reason for the orchestrator.
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._ray.shutdown)
            self._we_initialised_ray = False

    async def open(self, plan: RunPlan) -> None:
        """Initialise Ray + build the remote-function handle. The
        Protocol contract is open-once-per-run; the orchestrator's plan
        carries the price-table + secondary-sink settings we mirror into
        the aggregator. Ray-side resources (the connection, the actor
        registry) live with the Ray runtime; we just hold handles."""
        warn_if_local_files_with_distributed(plan, "ray executor")
        self._plan = plan
        self._accumulator = CostAccumulator()
        self._aggregator = SummaryAggregator(plan=plan)
        if plan.price_table is not None:
            warn_default_table_in_use(plan.price_table)

        # ray.init is fail-loud if [ray] is installed but cluster init
        # fails — we don't trap into a skip here; the test gate decides
        # via importorskip + a small probe whether to run integration tests.
        if not self._ray.is_initialized():
            init_kwargs: dict[str, Any] = {}
            if self._address is not None:
                init_kwargs["address"] = self._address
            if self._runtime_env is not None:
                init_kwargs["runtime_env"] = self._runtime_env
            if self._object_store_memory is not None:
                init_kwargs["object_store_memory"] = self._object_store_memory
            await asyncio.to_thread(self._ray.init, **init_kwargs)
            self._we_initialised_ray = True

        self._remote_fn = self._build_remote_function()
        # Kept for symmetry with LocalExecutor — Ray itself owns task
        # lifetimes, so there's nothing to enter here today.
        self._exit_stack = AsyncExitStack()

    def bind_cells(
        self,
        cells: list[tuple[CellDescriptor, EvalCase, RunVariant]],
    ) -> None:
        """Index ``(case, variant)`` by ``cell_id`` so ``submit_cell`` can
        populate the per-cell ``case_dict`` / ``eval_config_dict`` the
        worker rehydrates from. The runner builds cells with these dicts
        empty (the in-process executor doesn't need them); distributed
        executors fill them on the way out."""
        for cell, case, variant in cells:
            self._cell_index[cell.cell_id] = (case, variant)

    # ---- dispatch ------------------------------------------------------

    async def submit_cell(self, cell: CellDescriptor) -> Any:
        """Submit one Ray task. Returns the ``ObjectRef`` the caller
        awaits later via ``await_outcome``."""
        fn = self._require_function()
        payload = self._payload_for(cell)
        return await asyncio.to_thread(fn.remote, payload)

    async def dispatch_all(self, cells: list[CellDescriptor]) -> list[CellOutcome]:
        handles = await asyncio.gather(*(self.submit_cell(c) for c in cells))
        outcomes = await self.await_all(handles)
        for cell, outcome in zip(cells, outcomes, strict=True):
            self._ingest(cell, outcome)
        return outcomes

    async def await_outcome(self, handle: Any) -> CellOutcome:
        """Block (async) until the Ray task completes and rehydrate the
        worker's dict response into a ``CellOutcome``."""
        raw = await asyncio.to_thread(self._ray.get, handle)
        return self._outcome_from_dict(raw)

    async def await_all(self, handles: list[Any]) -> list[CellOutcome]:
        outcomes = await gather_outcomes(self, handles)
        return [o for o in outcomes]

    async def close(self) -> None:
        return None

    # ---- aggregation ---------------------------------------------------

    def finalize(self) -> RunSummary:
        if self._aggregator is None:
            raise RuntimeError("RayExecutor.finalize called before open()")
        summary = self._aggregator.finalize()
        summary.sink_errors = self._sink_errors
        return summary

    @property
    def sink_errors(self) -> list[dict[str, Any]]:
        return self._sink_errors

    # ---- internals -----------------------------------------------------

    def _ingest(self, cell: CellDescriptor, outcome: CellOutcome) -> None:
        if self._aggregator is None:
            raise RuntimeError("RayExecutor._ingest called before open()")
        self._aggregator.add(outcome)

    def _outcome_from_dict(self, raw: dict[str, Any]) -> CellOutcome:
        from eval_harness.runner.run_eval import CellOutcome

        cell_id = raw["cell_id"]
        case, variant = self._cell_index[cell_id]
        trace = Trace.model_validate(raw["trace"])
        results = [
            EvaluationResult.model_validate(r) for r in raw.get("results", [])
        ]
        return CellOutcome(case=case, variant=variant, trace=trace, results=results)

    def _payload_for(self, cell: CellDescriptor) -> dict[str, Any]:
        """Serialise the cell + populate the worker's rehydration dicts
        from the bound (case, variant) and the plan's full config. The
        runner intentionally leaves these empty so the in-process path
        doesn't deep-copy every case for nothing.

        Only fills empty dicts — callers that pre-populated the cell's
        ``case_dict`` / ``eval_config_dict`` (typically tests, but also
        any orchestrator that wants per-cell config slicing) win."""
        payload = cell.model_dump(mode="json")
        case, _variant = self._cell_index[cell.cell_id]
        if not payload.get("case_dict"):
            payload["case_dict"] = case.model_dump(mode="json")
        if not payload.get("eval_config_dict") and self._plan is not None:
            payload["eval_config_dict"] = self._plan.config.model_dump(mode="json")
        return payload

    def _require_function(self) -> Any:
        if self._remote_fn is None:
            raise RuntimeError(
                "RayExecutor: remote function not initialised — call open(plan) first"
            )
        return self._remote_fn

    def _build_remote_function(self) -> Any:
        """Wrap ``worker_run_cell_sync`` as a ``ray.remote`` task. Module-
        level qualified-name ensures Ray serialises by reference, not by
        pickling a closure — that's the rebuild-from-entry-points
        contract."""
        remote_kwargs: dict[str, Any] = {"num_cpus": self._num_cpus_per_cell}
        if self._num_gpus_per_cell is not None:
            remote_kwargs["num_gpus"] = self._num_gpus_per_cell

        def _evalh_worker(cell_dict: dict[str, Any]) -> dict[str, Any]:
            return worker_run_cell_sync(cell_dict, timeout_seconds=None)

        # Mirrors the modal_executor decorator-cast pattern: under
        # `mypy --strict` with ray installed the decorator chain is
        # typed via stubs the cast narrows; without ray installed
        # `ignore_missing_imports = true` makes everything `Any` and
        # the cast is a no-op rather than an `[unused-ignore]` violation.
        return cast(
            Callable[[dict[str, Any]], Any],
            self._ray.remote(**remote_kwargs)(_evalh_worker),
        )


__all__ = ["RayExecutor"]
