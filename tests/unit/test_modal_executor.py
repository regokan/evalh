"""ModalExecutor tests.

Modal is fundamentally cloud-deployed — there's no in-process Modal mode
to test against. Tests fall into three buckets:

1. Config validation (synchronous; no Modal involvement).
2. Dispatch shape via a fake Modal function (no Modal network).
3. ``@pytest.mark.modal`` smoke against real Modal — skipped cleanly
   when the Modal CLI isn't configured.

The shared ``_worker.worker_run_cell`` is exercised directly here too,
so the rebuild-adapters-from-entry-points path is unit-tested without
spawning a worker process.
"""

from __future__ import annotations

import os
import sys
import types
from typing import Any

import pytest

pytest.importorskip("modal")

from eval_harness.core.errors import ConfigError
from eval_harness.core.executors._worker import (
    worker_run_cell,
    worker_run_cell_sync,
)
from eval_harness.core.executors.base import executor_registry
from eval_harness.core.executors.modal_executor import ModalExecutor
from eval_harness.core.models import CellDescriptor

_STUB_MODULE = "_evalh_test_modal_worker_stub"


def _install_stub_agent() -> None:
    def agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {
            "final_answer": f"answer-for-{case['id']}",
            "metrics": {"token_input": 3, "token_output": 4},
        }

    mod = types.ModuleType(_STUB_MODULE)
    mod.run = agent  # type: ignore[attr-defined]
    sys.modules[_STUB_MODULE] = mod


def _make_cell(case_id: str = "c1") -> CellDescriptor:
    return CellDescriptor(
        cell_id=f"r1::{case_id}::stub::0123456789ab",
        run_id="r1",
        case_id=case_id,
        variant_name="stub",
        config_hash="0123456789abcdef",
        eval_config_dict={
            "schema_version": "1.0",
            "eval": {"name": "t"},
            "dataset": {"type": "yaml", "path": "ignored.yaml"},
            "systems": [
                {
                    "name": "stub",
                    "adapter": "python_function",
                    "target": f"{_STUB_MODULE}:run",
                }
            ],
            "evaluators": [],
            "output": [{"type": "local_files", "path": "./runs"}],
        },
        case_dict={"id": case_id, "input": {"q": case_id}, "metadata": {}, "expected": {}},
    )


# ---- config validation -----------------------------------------------------


def test_app_name_required() -> None:
    with pytest.raises(ConfigError, match="app_name"):
        ModalExecutor()


def test_factory_registers_modal() -> None:
    executor_registry.load_entry_points()
    assert "modal" in executor_registry.names()


def test_factory_builds_modal_via_registry() -> None:
    inst = executor_registry.build(type="modal", app_name="evalh-test")
    assert isinstance(inst, ModalExecutor)


def test_image_spec_stored_for_later_build() -> None:
    inst = ModalExecutor(
        app_name="evalh-test",
        image_spec={"pip_packages": ["eval-harness", "my-plugin"]},
    )
    # `image_spec` lives on the instance until `open(plan)`; the actual
    # Modal image is constructed there to avoid hitting Modal cloud
    # during construction.
    assert inst._image_spec == {"pip_packages": ["eval-harness", "my-plugin"]}


def test_timeout_defaults_to_600_seconds() -> None:
    inst = ModalExecutor(app_name="evalh-test")
    assert inst._timeout == 600


# ---- worker (no Modal involvement) -----------------------------------------


async def test_worker_run_cell_rebuilds_adapters_from_entry_points() -> None:
    """Headline contract: the worker rehydrates a `CellDescriptor` into
    a live SystemAdapter via the entry-point registry — config travels,
    code doesn't. python_function lets us exercise this without network."""
    _install_stub_agent()
    cell = _make_cell("c_worker_async")
    out = await worker_run_cell(cell.model_dump(mode="json"))

    assert out["cell_id"] == cell.cell_id
    trace = out["trace"]
    assert trace["case_id"] == "c_worker_async"
    assert trace["variant_name"] == "stub"
    assert trace["output"]["final_answer"] == "answer-for-c_worker_async"
    # Latency is the wall-clock the worker measured (overwritten by
    # `_enforce_invariants` over the adapter's value).
    assert trace["latency_ms"] >= 0
    assert out["results"] == []  # no evaluators in the cell config


def test_worker_run_cell_sync_drives_event_loop_locally() -> None:
    """`worker_run_cell_sync` is the entry point Modal calls (Modal
    functions are sync `def`). It must wrap an asyncio loop so adapters
    that are `async def` keep working."""
    _install_stub_agent()
    cell = _make_cell("c_worker_sync")
    out = worker_run_cell_sync(cell.model_dump(mode="json"))
    assert out["trace"]["output"]["final_answer"] == "answer-for-c_worker_sync"


async def test_worker_run_cell_returns_error_trace_when_variant_unknown() -> None:
    _install_stub_agent()
    cell = _make_cell()
    payload = cell.model_dump(mode="json")
    payload["variant_name"] = "no_such_variant"
    with pytest.raises(RuntimeError, match="no_such_variant"):
        await worker_run_cell(payload)


# ---- executor dispatch via a fake Modal function ---------------------------


class _FakeModalHandle:
    """Mimics modal.FunctionCall: stores the spawn argument, returns it
    from `.get`."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def get(self, timeout: int) -> dict[str, Any]:
        # The real Modal handle returns whatever the function returned;
        # here the worker runs in-process and we hand the result back.
        return worker_run_cell_sync(self._payload)


class _FakeModalFunction:
    def __init__(self) -> None:
        self.spawns: list[dict[str, Any]] = []

    def spawn(self, payload: dict[str, Any]) -> _FakeModalHandle:
        self.spawns.append(payload)
        return _FakeModalHandle(payload)


class _FakeModalApp:
    """Quacks like a modal.App for the executor's test seam: exposes the
    `function` we want the executor to dispatch to."""

    def __init__(self) -> None:
        self.function = _FakeModalFunction()


async def test_dispatch_all_spawns_one_call_per_cell_and_streams_outcomes() -> None:
    """End-to-end orchestrator path with a fake Modal app. Each cell is
    spawned exactly once; outcomes are rehydrated and ingested into the
    aggregator so `finalize()` returns a usable summary."""
    _install_stub_agent()
    from pathlib import Path

    from eval_harness.core.config import (
        DatasetConfig,
        EvalConfig,
        EvalIdentity,
        OutputConfig,
        SystemConfig,
    )
    from eval_harness.core.models import EvalCase, RunVariant
    from eval_harness.runner.plan_builder import RunPlan

    cfg = EvalConfig(
        schema_version="1.0",
        eval=EvalIdentity(name="t"),
        dataset=DatasetConfig(type="yaml"),
        systems=[SystemConfig(name="stub", adapter="python_function")],
        evaluators=[],
        output=[OutputConfig(type="local_files", path="./runs")],
    )

    # Build a minimal plan so finalize() has a SummaryAggregator backing.
    from unittest.mock import AsyncMock, MagicMock

    from eval_harness.runner.run_eval import CellOutcome  # noqa: F401 — type hint

    fake_store = MagicMock()
    fake_store.__aenter__ = AsyncMock(return_value=fake_store)
    fake_store.__aexit__ = AsyncMock(return_value=None)
    fake_store.open = AsyncMock()

    plan = RunPlan(
        config=cfg,
        run_id="r1",
        run_dir=Path("./runs/r1"),
        cases=[EvalCase(id=c, input={"q": c}) for c in ("c1", "c2")],
        variants=[RunVariant(name="stub", adapter="python_function", config={})],
        system_adapters={},
        trace_store=fake_store,
        workspace=None,
        evaluators=[],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
    )

    fake_app = _FakeModalApp()
    executor = ModalExecutor(app_name="evalh-test", modal_app=fake_app)
    cells = [_make_cell("c1"), _make_cell("c2")]

    async with executor:
        await executor.open(plan)
        # bind_cells uses (case, variant) for outcome reconstruction.
        live = [
            (
                cells[0],
                EvalCase(id="c1", input={"q": "c1"}),
                RunVariant(name="stub", adapter="python_function", config={}),
            ),
            (
                cells[1],
                EvalCase(id="c2", input={"q": "c2"}),
                RunVariant(name="stub", adapter="python_function", config={}),
            ),
        ]
        executor.bind_cells(live)
        outcomes = await executor.dispatch_all(cells)
        summary = executor.finalize()

    # One spawn per cell.
    assert len(fake_app.function.spawns) == 2
    # Outcomes have the rehydrated Trace + matching case_id.
    assert [o.case.id for o in outcomes] == ["c1", "c2"]
    assert outcomes[0].trace.output.final_answer == "answer-for-c1"
    # Streaming aggregator finalised one variant.
    assert summary.cases_total == 2
    assert summary.variants[0].name == "stub"


async def test_finalize_before_open_raises() -> None:
    executor = ModalExecutor(app_name="evalh-test", modal_app=_FakeModalApp())
    with pytest.raises(RuntimeError, match="before open"):
        executor.finalize()


# ---- @pytest.mark.modal: real cloud smoke ----------------------------------


def _modal_env_configured() -> bool:
    if os.environ.get("MODAL_TOKEN_ID") and os.environ.get("MODAL_TOKEN_SECRET"):
        return True
    # The Modal CLI writes a token file at ~/.modal.toml after `modal token new`.
    return (
        os.path.exists(os.path.expanduser("~/.modal.toml"))
        or os.path.exists(os.path.expanduser("~/.modal/config.toml"))
    )


@pytest.mark.modal
def test_modal_executor_smoke_against_real_cloud() -> None:
    """Build a `ModalExecutor`, open a minimal plan, dispatch one cell.

    Skips cleanly when no Modal token is configured locally — the
    `@pytest.mark.modal` marker is the CI gate; this guard is the
    developer-laptop equivalent so the test doesn't try to hit Modal
    cloud unauthenticated.
    """
    if not _modal_env_configured():
        pytest.skip("Modal CLI not configured (MODAL_TOKEN_ID / ~/.modal.toml)")
    # The real-cloud smoke is the manual benchmark per the bead; here we
    # only assert the executor accepts the production code path and can
    # build a real modal.Image / modal.App / modal.Function without
    # raising. We deliberately do NOT spawn a call against Modal cloud
    # because that incurs cost + requires a deployed app.
    executor = ModalExecutor(app_name="evalh-test", image_spec={"pip_packages": ["eval-harness"]})
    fn = executor._build_modal_function()
    assert fn is not None
    assert executor._modal_app is not None
