"""ModalExecutor — Modal cloud functions as the dispatch primitive.

Each cell becomes a ``modal.Function`` call. The orchestrator submits
serialised ``CellDescriptor`` dicts; the worker rebuilds adapters from
entry-points (via ``_worker.worker_run_cell_sync``), runs the cell, and
returns the outcome dict back to the orchestrator.

Modal is fundamentally cloud-deployed — there's no in-process Modal mode
to test against. The orchestrator-side logic (config validation, handle
shape, dispatch fan-out, outcome rehydration) is unit-testable without
hitting Modal cloud by injecting a fake ``modal_app``.

See docs/Executors.md for the deployment recipe (image_spec, secret
mounting, Modal CLI auth).
"""

from __future__ import annotations

import asyncio
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


_DEFAULT_TIMEOUT_SECONDS = 600


class ModalExecutor:
    """Dispatches each cell as a Modal function call.

    Config:
    - ``app_name`` (str, required): Modal app name; users typically
      pre-deploy the app once and have many runs hit the same name.
    - ``image_spec`` (dict, optional): forwarded to ``modal.Image.debian_slim
      ().pip_install(...)`` or equivalent. The default builds an image
      with ``eval-harness`` plus the consumer's project (the worker has
      to be able to ``import`` the same plugin packages the orchestrator
      installed locally — that's the entry-point contract).
    - ``timeout`` (int, default 600): per-call timeout, forwarded to
      ``modal.Function``.
    - ``modal_app`` (Any, test seam): a pre-built Modal app + function
      pair. Production callers leave this `None`; the executor builds
      its own at ``open(plan)``.
    """

    def __init__(
        self,
        *,
        app_name: str | None = None,
        image_spec: dict[str, Any] | None = None,
        timeout: int = _DEFAULT_TIMEOUT_SECONDS,
        modal_app: Any | None = None,
        **_extra: Any,
    ) -> None:
        if not app_name:
            raise ConfigError(
                "modal executor: 'app_name' (str) is required"
            )
        # Lazy SDK import: a ConfigError at construction time is friendlier
        # than an obscure import failure deep in the orchestrator.
        if modal_app is None:
            try:
                import modal  # noqa: F401
            except ImportError as e:
                raise ConfigError(
                    "modal executor requires the `modal` package. Install "
                    "with: pip install 'eval-harness[modal]'"
                ) from e

        self._app_name = app_name
        self._image_spec = dict(image_spec or {})
        self._timeout = int(timeout)
        # Test-injected modal app / function pair: ``modal_app`` must expose a
        # ``.function`` attribute that's callable as ``function.spawn(payload)``
        # returning a handle with ``.get(timeout)`` (mirrors Modal's real API).
        self._modal_app = modal_app
        self._modal_function: Any = (
            modal_app.function if modal_app is not None else None
        )

        # Per-run state, populated in `open(plan)`.
        self._plan: RunPlan | None = None
        self._accumulator: CostAccumulator | None = None
        self._aggregator: SummaryAggregator | None = None
        self._sink_errors: list[dict[str, Any]] = []
        self._cell_index: dict[str, tuple[EvalCase, RunVariant]] = {}
        self._exit_stack: AsyncExitStack | None = None

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
        """Build per-run state. The Modal app + function are constructed
        here when the caller didn't inject one — production wires the
        function once per run so the timeout / image_spec can be plan-
        derived (e.g. the orchestrator's price-table path uploaded as
        a Modal secret)."""
        warn_if_local_files_with_distributed(plan, "modal executor")
        self._plan = plan
        self._accumulator = CostAccumulator()
        self._aggregator = SummaryAggregator(plan=plan)
        if plan.price_table is not None:
            warn_default_table_in_use(plan.price_table)
        if self._modal_function is None:
            self._modal_function = self._build_modal_function()
        # No AsyncExitStack juggling for now — Modal-side resources (the
        # function, the app) live with Modal cloud; the orchestrator just
        # holds handles. The stack is kept for symmetry with LocalExecutor.
        self._exit_stack = AsyncExitStack()

    def bind_cells(
        self,
        cells: list[tuple[CellDescriptor, EvalCase, RunVariant]],
    ) -> None:
        """Store the per-cell live bindings so ``finalize`` can stream
        outcomes through the aggregator with the right (case, variant)
        pair. Modal workers don't read these — they rehydrate from
        ``cell.case_dict`` / ``cell.eval_config_dict`` themselves."""
        for cell, case, variant in cells:
            self._cell_index[cell.cell_id] = (case, variant)

    # ---- dispatch ------------------------------------------------------

    async def submit_cell(self, cell: CellDescriptor) -> Any:
        """Spawn one Modal function call. Returns the Modal handle the
        caller awaits later via ``await_outcome``."""
        fn = self._require_function()
        payload = cell.model_dump(mode="json")
        return await asyncio.to_thread(fn.spawn, payload)

    async def dispatch_all(self, cells: list[CellDescriptor]) -> list[CellOutcome]:
        """Spawn every cell, then gather. Modal already batches its RPC
        layer so two adjacent ``spawn`` calls coalesce into one
        round-trip when possible."""
        handles = await asyncio.gather(*(self.submit_cell(c) for c in cells))
        outcomes = await self.await_all(handles)
        # Stream rehydrated outcomes into the aggregator in input order so
        # the streaming-summary discipline still applies.
        for cell, outcome in zip(cells, outcomes, strict=True):
            self._ingest(cell, outcome)
        return outcomes

    async def await_outcome(self, handle: Any) -> CellOutcome:
        """Block (async) until the Modal call completes; rehydrate the
        worker's dict response into a `CellOutcome`."""
        raw = await asyncio.to_thread(handle.get, self._timeout)
        return self._outcome_from_dict(raw)

    async def await_all(self, handles: list[Any]) -> list[CellOutcome]:
        outcomes = await gather_outcomes(self, handles)
        # `gather_outcomes` returns Any to keep the Protocol open; we
        # know the concrete shape here.
        return [o for o in outcomes]

    async def close(self) -> None:
        return None

    # ---- aggregation ---------------------------------------------------

    def finalize(self) -> RunSummary:
        if self._aggregator is None:
            raise RuntimeError("ModalExecutor.finalize called before open()")
        summary = self._aggregator.finalize()
        summary.sink_errors = self._sink_errors
        return summary

    @property
    def sink_errors(self) -> list[dict[str, Any]]:
        return self._sink_errors

    # ---- internals -----------------------------------------------------

    def _ingest(self, cell: CellDescriptor, outcome: CellOutcome) -> None:
        if self._aggregator is None:
            raise RuntimeError("ModalExecutor._ingest called before open()")
        self._aggregator.add(outcome)

    def _outcome_from_dict(self, raw: dict[str, Any]) -> CellOutcome:
        """Convert the worker's wire-format dict back into a
        `CellOutcome`. Looking up case + variant from the bound index
        means workers don't have to ship them back."""
        from eval_harness.runner.run_eval import CellOutcome

        cell_id = raw["cell_id"]
        case, variant = self._cell_index[cell_id]
        trace = Trace.model_validate(raw["trace"])
        results = [
            EvaluationResult.model_validate(r) for r in raw.get("results", [])
        ]
        return CellOutcome(case=case, variant=variant, trace=trace, results=results)

    def _require_function(self) -> Any:
        if self._modal_function is None:
            raise RuntimeError(
                "ModalExecutor: function not initialised — call open(plan) first"
            )
        return self._modal_function

    def _build_modal_function(self) -> Any:
        """Construct a Modal app + remote function from the configured
        ``app_name`` + ``image_spec``. Kept in its own method so tests
        injecting a fake app via ``modal_app=`` never touch this path."""
        import modal

        image = self._compose_image(modal)
        app = modal.App(self._app_name, image=image)

        def _evalh_worker(cell_dict: dict[str, Any]) -> dict[str, Any]:
            # Module-level qualified name so Modal can pickle by reference.
            return worker_run_cell_sync(cell_dict, timeout_seconds=None)

        # Apply the decorator post-def with a `cast` (mirrors ev-joq's
        # `_platforms/otel.py` approach). Under mypy --strict, with the
        # [modal] extra absent, `modal.App.function` is `Any`; an untyped
        # decorator applied to a typed function produces an
        # `[untyped-decorator]` error. `cast` is honoured under both env
        # profiles: with `modal` installed mypy narrows the decorator
        # result to the typed signature, without it `ignore_missing_imports
        # = true` makes the cast target itself `Any` so the cast is a
        # no-op rather than an `[unused-ignore]` violation.
        decorated = cast(
            Callable[[dict[str, Any]], dict[str, Any]],
            app.function(timeout=self._timeout)(_evalh_worker),
        )

        # Stash the app so callers / tests can inspect it.
        self._modal_app = app
        return decorated

    def _compose_image(self, modal_mod: Any) -> Any:
        """Build a ``modal.Image`` from ``image_spec``. Conservative
        defaults: a debian-slim base with the locally-installed
        ``eval-harness`` version. Users override via ``image_spec``."""
        spec = self._image_spec
        base = (
            modal_mod.Image.from_registry(spec["base"])
            if "base" in spec
            else modal_mod.Image.debian_slim()
        )
        pip_packages = spec.get("pip_packages") or ["eval-harness"]
        if pip_packages:
            base = base.pip_install(*pip_packages)
        env = spec.get("env")
        if isinstance(env, dict):
            base = base.env(env)
        return base


__all__ = ["ModalExecutor"]
