"""Tests for the Executor Protocol + registry.

The Local executor (F2) and distributed executors (Modal / K8s /
Celery / Ray) live in subsequent beads. This bead lands only the
Protocol + the entry-point group + the registry seam; the tests
exercise the registry surface against a tiny fake executor.
"""

from __future__ import annotations

from typing import Any

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.executors import (
    CellHandle,
    Executor,
    ExecutorRegistry,
    executor_registry,
)
from eval_harness.core.executors.base import gather_outcomes
from eval_harness.core.models import CellDescriptor


class _FakeExecutor:
    """Minimal Executor for protocol-level tests. Returns a fixed
    `CellOutcome`-shaped dict per submit so the registry-level tests
    don't depend on the v2 runner integration."""

    def __init__(self) -> None:
        self.opened = False
        self.closed = False
        self.submitted: list[CellDescriptor] = []

    async def __aenter__(self) -> _FakeExecutor:
        return self

    async def __aexit__(self, *a: object) -> None:
        return None

    async def open(self, plan: Any) -> None:
        self.opened = True

    def bind_cells(self, cells: list[tuple[CellDescriptor, Any, Any]]) -> None:
        return None

    def finalize(self) -> dict[str, str]:
        return {"summary": "fake"}

    async def submit_cell(self, cell: CellDescriptor) -> CellHandle:
        self.submitted.append(cell)
        return f"handle:{cell.cell_id}"

    async def dispatch_all(
        self, cells: list[CellDescriptor]
    ) -> list[dict[str, str]]:
        handles = [await self.submit_cell(c) for c in cells]
        return await self.await_all(handles)

    async def await_outcome(self, handle: CellHandle) -> dict[str, str]:
        return {"handle": str(handle), "status": "ok"}

    async def await_all(self, handles: list[CellHandle]) -> list[dict[str, str]]:
        return await gather_outcomes(self, handles)  # type: ignore[return-value]

    async def close(self) -> None:
        self.closed = True


def _cell(cell_id: str = "r1::c1::v1::deadbeef0123") -> CellDescriptor:
    return CellDescriptor(
        cell_id=cell_id,
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        config_hash="deadbeef0123",
        eval_config_dict={},
        case_dict={"id": "c1", "input": {}},
    )


# ---- Protocol runtime check --------------------------------------------


def test_fake_executor_satisfies_protocol() -> None:
    assert isinstance(_FakeExecutor(), Executor)


# ---- Registry seam ------------------------------------------------------


def test_executor_registry_dispatch() -> None:
    registry = ExecutorRegistry()
    fake = _FakeExecutor()
    registry.register("test_local", lambda: fake)
    assert registry.resolve("test_local") is fake


def test_executor_registry_unknown_raises_with_install_hint() -> None:
    registry = ExecutorRegistry()
    with pytest.raises(ConfigError) as exc:
        registry.resolve("never-registered")
    msg = str(exc.value)
    assert "never-registered" in msg
    # The error points at F2 (where Local lands) and the entry-point group.
    assert "eval_harness.executors" in msg


def test_executor_registry_load_entry_points_idempotent() -> None:
    registry = ExecutorRegistry()
    registry.load_entry_points()
    snapshot = list(registry.names())
    registry.load_entry_points()
    assert list(registry.names()) == snapshot


def test_executor_registry_unregister() -> None:
    registry = ExecutorRegistry()
    registry.register("toy", lambda: _FakeExecutor())
    assert "toy" in registry.names()
    registry.unregister("toy")
    assert "toy" not in registry.names()


def test_entry_point_group_declared() -> None:
    """The `eval_harness.executors` group must be discoverable even
    though no built-in executors register yet."""
    from importlib.metadata import entry_points

    eps = entry_points(group="eval_harness.executors")
    assert eps is not None
    # The global registry must accept load_entry_points without error.
    executor_registry.load_entry_points()


# ---- Lifecycle ----------------------------------------------------------


async def test_executor_full_lifecycle() -> None:
    executor = _FakeExecutor()
    await executor.open(plan=None)
    handle = await executor.submit_cell(_cell())
    outcome = await executor.await_outcome(handle)
    await executor.close()

    assert executor.opened
    assert executor.closed
    assert len(executor.submitted) == 1
    assert outcome["status"] == "ok"
    assert outcome["handle"] == f"handle:{_cell().cell_id}"


async def test_executor_await_all_returns_one_outcome_per_handle() -> None:
    executor = _FakeExecutor()
    handles = [
        await executor.submit_cell(_cell(cell_id=f"r1::c{i}::v1::abc123def456"))
        for i in range(5)
    ]
    outcomes = await executor.await_all(handles)
    assert len(outcomes) == 5
    assert {o["status"] for o in outcomes} == {"ok"}


# ---- Workers-rebuild-from-config (NOT pickled adapters) ---------------


def test_cell_descriptor_carries_config_not_live_objects() -> None:
    """The contract is that workers rebuild adapters from
    `eval_config_dict` via the factory layer. The descriptor must be
    JSON-serializable end-to-end — if a future change adds a live
    object (e.g. a constructed adapter), this test catches it."""
    import json

    desc = _cell()
    body = desc.model_dump_json()
    rehydrated = json.loads(body)
    # The shape stays a plain JSON tree (no class references, no
    # pickled objects).
    assert isinstance(rehydrated["eval_config_dict"], dict)
    assert isinstance(rehydrated["case_dict"], dict)
    # And re-validates back into a CellDescriptor identical to the
    # original — symmetry that the v2 wire protocol depends on.
    assert CellDescriptor.model_validate(rehydrated) == desc
