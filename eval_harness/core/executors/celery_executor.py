"""CeleryExecutor — Celery tasks as the dispatch primitive.

Each cell becomes a ``task.apply_async`` call against a Celery app
backed by a broker (Redis or AMQP) and a result backend. The
orchestrator submits a serialised ``CellDescriptor`` payload; the
Celery worker calls ``_worker.worker_run_cell_sync``, which rebuilds
adapters + evaluators from entry-points and returns an outcome dict.

Celery is fundamentally broker-deployed — there is no in-process
"local" mode that round-trips through the broker. The orchestrator-side
logic (config validation, dispatch fan-out, outcome rehydration) is
unit-testable without a live broker by injecting a fake app via the
``celery_app`` seam.

See docs/Executors.md for the deployment recipe (broker URL, worker
boot command, image build).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, cast

from eval_harness.core.errors import ConfigError
from eval_harness.core.executors._worker import worker_run_cell_sync
from eval_harness.core.executors.base import gather_outcomes
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


_DEFAULT_TASK_NAME = "evalh.run_cell"
_DEFAULT_GET_TIMEOUT_SECONDS = 600


class CeleryExecutor:
    """Dispatches each cell as a Celery task.

    Config:
    - ``broker_url`` (str, required): e.g. ``"redis://localhost:6379/0"``
      or ``"amqp://guest:guest@localhost//"``. Forwarded to ``Celery(...)``.
    - ``result_backend`` (str, optional): defaults to ``broker_url``.
      Redis works as both broker and result store; AMQP users typically
      set this to a Redis URL because AMQP-as-backend has known
      reliability issues.
    - ``task_name`` (str, default ``"evalh.run_cell"``): the name the
      task is registered under in the Celery app. Workers must boot
      against an app that registers the *same* task name — the worker
      process resolves the task by name, not by Python reference.
    - ``timeout`` (int, default 600): seconds passed to ``AsyncResult.get``.
    - ``celery_app`` (Any, test seam): a pre-built object exposing
      ``.send_task(name, args=..., kwargs=...)`` returning an object
      with ``.get(timeout=...)``. Production callers leave this
      ``None`` and the executor builds its own ``Celery`` instance at
      ``open(plan)``.
    """

    def __init__(
        self,
        *,
        broker_url: str | None = None,
        result_backend: str | None = None,
        task_name: str = _DEFAULT_TASK_NAME,
        timeout: int = _DEFAULT_GET_TIMEOUT_SECONDS,
        celery_app: Any | None = None,
        **_extra: Any,
    ) -> None:
        if celery_app is None and not broker_url:
            raise ConfigError(
                "celery executor: 'broker_url' (str) is required, e.g. "
                "'redis://localhost:6379/0' or 'amqp://guest:guest@localhost//'."
            )
        # Lazy SDK import: a ConfigError at construction time is friendlier
        # than an obscure ImportError deep in the orchestrator.
        if celery_app is None:
            try:
                import celery  # noqa: F401
            except ImportError as e:
                raise ConfigError(
                    "celery executor requires the `celery` package. Install "
                    "with: pip install 'eval-harness[celery]'"
                ) from e

        self._broker_url = broker_url
        self._result_backend = result_backend or broker_url
        self._task_name = task_name
        self._timeout = int(timeout)
        self._celery_app: Any = celery_app

        # Per-run state, populated in ``open(plan)``.
        self._plan: RunPlan | None = None
        self._accumulator: CostAccumulator | None = None
        self._aggregator: SummaryAggregator | None = None
        self._sink_errors: list[dict[str, Any]] = []
        self._cell_index: dict[str, tuple[EvalCase, RunVariant]] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._we_built_app = False

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

    async def open(self, plan: RunPlan) -> None:
        """Build per-run state. The Celery app is constructed here when
        the caller didn't inject one — production wires the app once per
        run so broker / result-backend URLs come straight from
        eval.yaml. Workers boot independently against the same broker;
        this side never spawns workers."""
        self._plan = plan
        self._accumulator = CostAccumulator()
        self._aggregator = SummaryAggregator(plan=plan)
        if plan.price_table is not None:
            warn_default_table_in_use(plan.price_table)
        if self._celery_app is None:
            self._celery_app = self._build_celery_app()
            self._we_built_app = True
        self._exit_stack = AsyncExitStack()

    def bind_cells(
        self,
        cells: list[tuple[CellDescriptor, EvalCase, RunVariant]],
    ) -> None:
        """Index ``(case, variant)`` by ``cell_id`` so ``await_outcome``
        can rebuild ``CellOutcome``s without re-validating per cell.
        Celery workers don't read this — they rehydrate from
        ``cell.case_dict`` / ``cell.eval_config_dict`` themselves."""
        for cell, case, variant in cells:
            self._cell_index[cell.cell_id] = (case, variant)

    # ---- dispatch ------------------------------------------------------

    async def submit_cell(self, cell: CellDescriptor) -> Any:
        """Dispatch one cell via ``celery_app.send_task``. Returns the
        ``AsyncResult`` the caller awaits later via ``await_outcome``.

        ``send_task`` (vs. ``task.apply_async`` on a bound task object)
        is the right primitive when the orchestrator doesn't actually
        own the task implementation — workers do. The orchestrator only
        names the task; routing + serialisation happen on Celery's side.
        """
        app = self._require_app()
        payload = cell.model_dump(mode="json")
        return await asyncio.to_thread(
            app.send_task, self._task_name, args=[payload]
        )

    async def dispatch_all(self, cells: list[CellDescriptor]) -> list[CellOutcome]:
        """Submit every cell, then gather. Per-cell ``send_task`` calls
        offload to a thread pool so a slow broker round-trip on one cell
        doesn't serialise the rest."""
        handles = await asyncio.gather(*(self.submit_cell(c) for c in cells))
        outcomes = await self.await_all(handles)
        for cell, outcome in zip(cells, outcomes, strict=True):
            self._ingest(cell, outcome)
        return outcomes

    async def await_outcome(self, handle: Any) -> CellOutcome:
        """Block (async) until the Celery task completes. ``AsyncResult.get``
        is sync + blocking; we run it in a thread to keep the event loop
        responsive."""
        raw = await asyncio.to_thread(handle.get, timeout=self._timeout)
        return self._outcome_from_dict(raw)

    async def await_all(self, handles: list[Any]) -> list[CellOutcome]:
        outcomes = await gather_outcomes(self, handles)
        return [o for o in outcomes]

    async def close(self) -> None:
        return None

    # ---- aggregation ---------------------------------------------------

    def finalize(self) -> RunSummary:
        if self._aggregator is None:
            raise RuntimeError("CeleryExecutor.finalize called before open()")
        summary = self._aggregator.finalize()
        summary.sink_errors = self._sink_errors
        return summary

    @property
    def sink_errors(self) -> list[dict[str, Any]]:
        return self._sink_errors

    # ---- internals -----------------------------------------------------

    def _ingest(self, cell: CellDescriptor, outcome: CellOutcome) -> None:
        if self._aggregator is None:
            raise RuntimeError("CeleryExecutor._ingest called before open()")
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

    def _require_app(self) -> Any:
        if self._celery_app is None:
            raise RuntimeError(
                "CeleryExecutor: celery app not initialised — call open(plan) first"
            )
        return self._celery_app

    def _build_celery_app(self) -> Any:
        """Construct a ``celery.Celery`` app + register the worker task.

        Registering ``worker_run_cell_sync`` here is what makes a single
        Python process able to act as both orchestrator and (with a
        worker started against the same broker + module) worker. In
        production deployments the worker process imports
        ``eval_harness.core.executors.celery_executor`` to get the same
        task registration; that's why the registration is module-level
        idempotent — Celery's ``@task(name=...)`` decorator no-ops on a
        repeat call with the same name."""
        import celery

        app = celery.Celery(
            "evalh",
            broker=self._broker_url,
            backend=self._result_backend,
        )

        # Register the cell-running task. The name is what workers + the
        # orchestrator agree on; the function body is the same shared
        # ``_worker.worker_run_cell_sync`` Modal and Ray use, so workers
        # behave identically across executors.
        def _evalh_run_cell(cell_dict: dict[str, Any]) -> dict[str, Any]:
            return worker_run_cell_sync(cell_dict, timeout_seconds=None)

        # Mirrors the modal_executor / ray_executor decorator-cast
        # pattern: under mypy --strict with the [celery] extra absent,
        # `app.task(name=...)` is `Any`; an untyped decorator applied to
        # a typed function produces an `[untyped-decorator]` error. The
        # `cast` is honoured under both env profiles (ignore_missing_imports
        # makes the target itself `Any` when celery isn't installed, so
        # the cast is a no-op rather than `[unused-ignore]`).
        decorated = cast(
            Callable[[dict[str, Any]], dict[str, Any]],
            app.task(name=self._task_name)(_evalh_run_cell),
        )

        # Stash so tests + diagnostics can introspect the registration.
        self._celery_task = decorated
        return app


__all__ = ["CeleryExecutor"]
