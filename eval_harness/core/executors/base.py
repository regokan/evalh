"""Executor Protocol + registry.

`Executor` is the v2 dispatch primitive. The runner builds a
`CellDescriptor` per (case, variant) and submits it; the executor
returns an opaque `CellHandle` the runner later awaits.

Built-in executors register via the `eval_harness.executors`
entry-point group. The group is declared empty for this bead — the
Local executor (F2) registers `local`; the distributed executors
(Modal / K8s / Celery / Ray) register from their own packages.

Mirrors the LlmBackend / TraceEnricher registry shape: callers
acquire via `executor_registry.resolve(name)` and the registry loads
entry-points lazily.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any, Protocol, Self, runtime_checkable

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import CellDescriptor

# Executors return opaque per-submission handles. `Any` is intentional:
# Local uses an asyncio.Task; Modal uses a function-call object; K8s
# Jobs uses a job name. The runner doesn't look inside.
CellHandle = Any


@runtime_checkable
class Executor(Protocol):
    """The unit of distribution is a cell. Executors carry cells.

    Lifecycle: ``async with executor`` (open enters per-run context);
    `open(plan)` once; then `dispatch_all(cells)` (or any number of
    `submit_cell` / `await_outcome` / `await_all` pairs); then
    `close()`. Implementations should be safe to call `submit_cell`
    concurrently; ordering is the caller's responsibility.

    The runner uses `dispatch_all` exclusively so it never has to
    branch on executor class. Implementations supply whatever
    per-environment dispatch makes sense (in-process `asyncio.gather`
    for Local, batched RPC for distributed back-ends).
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None: ...

    async def open(self, plan: Any) -> None:
        """Hand over the run plan so the executor can build whatever
        per-run state it needs (e.g. capacity-pool semaphores for the
        Local executor, queue connections for Celery)."""
        ...

    def bind_cells(self, cells: list[tuple[CellDescriptor, Any, Any]]) -> None:
        """Hand the executor the per-cell live bindings
        ``(cell, case, variant)``. The in-process Local executor uses
        these to skip pydantic re-validation per cell; distributed
        executors that rehydrate from `cell.case_dict` /
        `cell.eval_config_dict` can no-op."""
        ...

    def finalize(self) -> Any:
        """Return the aggregated `RunSummary` after `dispatch_all`
        completes. Implementations stream outcomes into the
        aggregator as cells finish so this is just the final flush."""
        ...

    async def dispatch_all(self, cells: list[CellDescriptor]) -> list[Any]:
        """Run every cell to completion, returning their `CellOutcome`s
        in input order. The runner calls this exactly once per run.

        Distributed back-ends typically implement this by submitting
        cells to a queue and gathering outcomes; the in-process Local
        executor implements it with a single ``asyncio.gather``. The
        runner doesn't care which — it only ever calls this method.
        """
        ...

    async def submit_cell(self, cell: CellDescriptor) -> CellHandle:
        """Submit one cell for execution. Returns an opaque handle the
        caller awaits later via `await_outcome` / `await_all`. Most
        callers use `dispatch_all` instead — this is the underlying
        primitive distributed executors expose for fine-grained
        scheduling integration."""
        ...

    async def await_outcome(self, handle: CellHandle) -> Any:
        """Block (asynchronously) until the cell's `CellOutcome` is
        available. Returns the outcome; raises only on executor-side
        failures that aren't covered by `Trace.error`."""
        ...

    async def await_all(self, handles: list[CellHandle]) -> list[Any]:
        """Convenience: gather every handle's outcome. The default
        implementation in `gather_outcomes` below is the obvious one;
        executors may override (e.g. for batched RPC backends)."""
        ...

    async def close(self) -> None:
        """Tear down per-run resources. Called from the runner's
        finally block."""
        ...


async def gather_outcomes(
    executor: Executor, handles: list[CellHandle]
) -> list[Any]:
    """Reference `await_all` implementation — executor subclasses can
    delegate here when they don't have a batched RPC to exploit."""
    return await asyncio.gather(*(executor.await_outcome(h) for h in handles))


ExecutorFactory = Callable[[], Executor]
_ENTRY_POINT_GROUP = "eval_harness.executors"


class ExecutorRegistry:
    """Maps executor name -> factory producing one `Executor`.

    Loads from the `eval_harness.executors` entry-point group on first
    `load_entry_points()` call. Direct `register(name, cls)` is also
    supported for tests + third-party code that prefers programmatic
    registration.
    """

    def __init__(self) -> None:
        self._factories: dict[str, ExecutorFactory] = {}
        self._entry_points_loaded = False

    def register(self, name: str, factory: ExecutorFactory) -> None:
        self._factories[name] = factory

    def unregister(self, name: str) -> None:
        self._factories.pop(name, None)

    def load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        for ep in entry_points(group=_ENTRY_POINT_GROUP):
            self._factories[ep.name] = ep.load()
        self._entry_points_loaded = True

    def resolve(self, name: str) -> Executor:
        factory = self._factories.get(name)
        if factory is None:
            raise ConfigError(
                f"executors: no executor registered as {name!r}. "
                f"Known: {sorted(self._factories)}. The Local executor "
                f"ships in v2 F2; distributed executors register from "
                f"their own packages via the `eval_harness.executors` "
                f"entry-point group."
            )
        return factory()

    def build(self, *, type: str, **kwargs: Any) -> Executor:
        """Resolve `type` and construct the executor with `**kwargs`.

        Used by the runner so it never has to ``isinstance`` a concrete
        executor or special-case `local`. Entry-point loading is
        lazy + idempotent on first call."""
        self.load_entry_points()
        factory = self._factories.get(type)
        if factory is None:
            raise ConfigError(
                f"run.executor.type: no executor registered as {type!r}. "
                f"Known: {sorted(self._factories)}. Distributed executors "
                f"register from their own packages via the "
                f"`eval_harness.executors` entry-point group."
            )
        return factory(**kwargs)

    def names(self) -> list[str]:
        return sorted(self._factories)


executor_registry = ExecutorRegistry()
