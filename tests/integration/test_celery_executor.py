"""CeleryExecutor integration tests.

Three buckets, mirroring ``test_ray_executor.py`` / ``test_modal_executor.py``:

1. Config validation + entry-point registration (synchronous; no Celery
   involvement).
2. Dispatch shape via a fake Celery app (no live broker). Proves the
   orchestrator-side fan-out + outcome rehydration without standing up
   Redis.
3. ``@pytest.mark.celery`` end-to-end against a real broker — gated by
   ``EVALH_TEST_REDIS_URL``. Mirrors the docker_volume / ray pattern:
   skip cleanly when the marker is excluded; fail loudly when the
   marker IS selected but the broker is unreachable.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("celery")

from eval_harness.core.config import (
    DatasetConfig,
    EvalConfig,
    EvalIdentity,
    OutputConfig,
    SystemConfig,
)
from eval_harness.core.errors import ConfigError
from eval_harness.core.executors._worker import worker_run_cell_sync
from eval_harness.core.executors.base import executor_registry
from eval_harness.core.executors.celery_executor import CeleryExecutor
from eval_harness.core.models import CellDescriptor, EvalCase, RunVariant
from eval_harness.runner.plan_builder import RunPlan

# Real importable stub agent. Workers (Celery's prefork pool runs a
# subprocess that has its own ``sys.modules``) need to resolve the
# python_function adapter's ``module:func`` target. The Ray executor
# already maintains this fixture; we reuse it so both executors share
# the same worker contract.
_STUB_MODULE = "ray_stub_agent"
_STUB_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _install_stub_agent() -> None:
    stub_dir = str(_STUB_DIR)
    if stub_dir not in sys.path:
        sys.path.insert(0, stub_dir)
    sys.modules.pop(_STUB_MODULE, None)


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


def _build_plan(case_ids: list[str]) -> RunPlan:
    cfg = EvalConfig(
        schema_version="1.0",
        eval=EvalIdentity(name="t"),
        dataset=DatasetConfig(type="yaml"),
        systems=[
            SystemConfig(
                name="stub",
                adapter="python_function",
                target=f"{_STUB_MODULE}:run",
            )
        ],
        evaluators=[],
        output=[OutputConfig(type="local_files", path="./runs")],
    )
    fake_store = MagicMock()
    fake_store.__aenter__ = AsyncMock(return_value=fake_store)
    fake_store.__aexit__ = AsyncMock(return_value=None)
    fake_store.open = AsyncMock()
    return RunPlan(
        config=cfg,
        run_id="r1",
        run_dir=Path("./runs/r1"),
        cases=[EvalCase(id=c, input={"q": c}) for c in case_ids],
        variants=[
            RunVariant(
                name="stub",
                adapter="python_function",
                config={"target": f"{_STUB_MODULE}:run"},
            )
        ],
        system_adapters={},
        trace_store=fake_store,
        workspace=None,
        evaluators=[],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
    )


# ---- config + registration --------------------------------------------------


def test_broker_url_required() -> None:
    with pytest.raises(ConfigError, match="broker_url"):
        CeleryExecutor()


def test_factory_registers_celery() -> None:
    executor_registry.load_entry_points()
    assert "celery" in executor_registry.names()


def test_factory_builds_celery_via_registry() -> None:
    inst = executor_registry.build(
        type="celery", broker_url="redis://localhost:6379/0"
    )
    assert isinstance(inst, CeleryExecutor)


def test_missing_celery_module_raises_config_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the ``[celery]`` extra is missing, construction should raise a
    helpful ``ConfigError`` instead of a raw ``ImportError`` deep inside
    the orchestrator."""
    import builtins

    real_import = builtins.__import__

    def deny_celery(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "celery":
            raise ImportError("no celery for you")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_celery)
    with pytest.raises(ConfigError, match=r"eval-harness\[celery\]"):
        CeleryExecutor(broker_url="redis://localhost:6379/0")


def test_result_backend_defaults_to_broker_url() -> None:
    inst = CeleryExecutor(broker_url="redis://localhost:6379/0")
    assert inst._result_backend == "redis://localhost:6379/0"


def test_result_backend_override_wins() -> None:
    inst = CeleryExecutor(
        broker_url="amqp://guest:guest@localhost//",
        result_backend="redis://localhost:6379/1",
    )
    assert inst._result_backend == "redis://localhost:6379/1"


def test_task_name_defaults_to_evalh_run_cell() -> None:
    inst = CeleryExecutor(broker_url="redis://localhost:6379/0")
    assert inst._task_name == "evalh.run_cell"


# ---- dispatch via a fake Celery app ----------------------------------------


class _FakeAsyncResult:
    """Mimics ``celery.result.AsyncResult``: stores the payload, returns
    the worker output from ``.get`` after running the worker locally."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def get(self, timeout: int | None = None) -> dict[str, Any]:
        # Real Celery returns whatever the worker computed; for the fake
        # we run the same shared worker entrypoint synchronously so the
        # round-trip shape matches.
        return worker_run_cell_sync(self._payload)


class _FakeCeleryApp:
    """Quacks like ``celery.Celery``: records send_task invocations,
    returns a fake AsyncResult."""

    def __init__(self) -> None:
        self.sends: list[tuple[str, list[Any], dict[str, Any]]] = []

    def send_task(
        self,
        name: str,
        args: list[Any] | None = None,
        kwargs: dict[str, Any] | None = None,
    ) -> _FakeAsyncResult:
        args = list(args or [])
        kwargs = dict(kwargs or {})
        self.sends.append((name, args, kwargs))
        # The orchestrator always passes the cell-dict payload as args[0].
        return _FakeAsyncResult(args[0])


async def test_dispatch_all_sends_one_task_per_cell_and_streams_outcomes() -> None:
    """Headline end-to-end: every cell becomes one Celery task; outcomes
    rehydrate through the bound (case, variant) index and ingest into
    the streaming aggregator so ``finalize()`` returns a summary."""
    _install_stub_agent()
    fake_app = _FakeCeleryApp()
    plan = _build_plan(["c1", "c2"])
    cells = [_make_cell("c1"), _make_cell("c2")]

    executor = CeleryExecutor(
        broker_url="redis://localhost:6379/0", celery_app=fake_app
    )
    async with executor:
        await executor.open(plan)
        executor.bind_cells(
            [
                (cells[0], plan.cases[0], plan.variants[0]),
                (cells[1], plan.cases[1], plan.variants[0]),
            ]
        )
        outcomes = await executor.dispatch_all(cells)
        summary = executor.finalize()

    assert len(fake_app.sends) == 2
    # Each send carries the registered task name + a payload with the cell dict.
    submitted_ids = {send[1][0]["case_dict"]["id"] for send in fake_app.sends}
    assert submitted_ids == {"c1", "c2"}
    for name, _args, _kwargs in fake_app.sends:
        assert name == "evalh.run_cell"

    assert [o.case.id for o in outcomes] == ["c1", "c2"]
    assert outcomes[0].trace.output.final_answer == "ray-answer-for-c1"
    assert summary.cases_total == 2
    assert summary.variants[0].name == "stub"


async def test_custom_task_name_is_used_for_send_task() -> None:
    """``task_name`` config controls the name the orchestrator dispatches
    to. Workers boot against an app that registers the same name."""
    _install_stub_agent()
    fake_app = _FakeCeleryApp()
    plan = _build_plan(["c1"])
    cells = [_make_cell("c1")]
    executor = CeleryExecutor(
        broker_url="redis://localhost:6379/0",
        celery_app=fake_app,
        task_name="my.org.run_cell",
    )
    async with executor:
        await executor.open(plan)
        executor.bind_cells([(cells[0], plan.cases[0], plan.variants[0])])
        await executor.dispatch_all(cells)

    assert fake_app.sends[0][0] == "my.org.run_cell"


async def test_finalize_before_open_raises() -> None:
    executor = CeleryExecutor(
        broker_url="redis://localhost:6379/0", celery_app=_FakeCeleryApp()
    )
    with pytest.raises(RuntimeError, match="before open"):
        executor.finalize()


async def test_submit_before_open_raises() -> None:
    """``submit_cell`` without ``open(plan)`` should fail with a clear
    runtime error rather than ``AttributeError`` on the missing app."""
    # No app injected, no open — the executor has nothing wired up.
    executor = CeleryExecutor(
        broker_url="redis://localhost:6379/0", celery_app=None
    )
    # Clear the lazy-built app so submit_cell sees the uninitialised state.
    executor._celery_app = None
    with pytest.raises(RuntimeError, match="not initialised"):
        await executor.submit_cell(_make_cell("c1"))


# ---- worker rebuild contract (no Celery; the worker is shared) -------------


def test_worker_run_cell_sync_rebuilds_adapters_from_config() -> None:
    """The Celery worker entry point is the same ``worker_run_cell_sync``
    Modal + Ray use. Workers receive *only* config + case dict — they
    rebuild adapters via the entry-point registry."""
    _install_stub_agent()
    cell = _make_cell("c_rebuild")
    out = worker_run_cell_sync(cell.model_dump(mode="json"))
    assert out["trace"]["output"]["final_answer"] == "ray-answer-for-c_rebuild"
    assert out["trace"]["case_id"] == "c_rebuild"
    assert out["trace"]["variant_name"] == "stub"


# ---- @pytest.mark.celery: real broker smoke --------------------------------


def _redis_url() -> str | None:
    """The marker gate. ``EVALH_TEST_REDIS_URL`` is the explicit opt-in
    for the live-broker tests; without it we don't try to hit any local
    Redis (and the marker exclusion in CI already keeps the test out)."""
    return os.environ.get("EVALH_TEST_REDIS_URL")


def _broker_reachable(url: str) -> bool:
    """Sanity-probe the broker before standing up Celery's worker
    process. If the URL points at nothing the test fails loudly per the
    docker_volume / ray gate pattern; the marker is the CI gate."""
    try:
        import redis

        client = redis.Redis.from_url(url, socket_connect_timeout=2)
        client.ping()
        return True
    except Exception:
        return False


def _start_inproc_worker(app: Any) -> threading.Thread:
    """Boot a Celery worker inside this process via the threading-pool
    backend. ``solo`` runs the consumer loop on the worker thread so
    we don't fork — no orphan processes if the test crashes. Returns
    the thread so the test can keep a handle for shutdown."""
    # Worker startup is blocking on `worker.start()`; we run it in a thread
    # and stop it via app.control after the test completes.
    def _run() -> None:
        worker = app.Worker(
            pool="solo",
            concurrency=1,
            loglevel="ERROR",
            without_heartbeat=True,
            without_gossip=True,
            without_mingle=True,
        )
        worker.start()

    t = threading.Thread(target=_run, name="evalh-celery-worker", daemon=True)
    t.start()
    return t


@pytest.mark.celery
async def test_celery_executor_dispatch_end_to_end_against_live_broker() -> None:
    """Spin up a real Celery app against ``EVALH_TEST_REDIS_URL``,
    register the worker task, dispatch a small fixture, verify outcomes
    rehydrate to the right value. Mirrors the ``test_ray_executor``
    end-to-end test — same shape, different transport."""
    url = _redis_url()
    if url is None:
        pytest.fail(
            "EVALH_TEST_REDIS_URL is unset but @pytest.mark.celery was "
            "selected. Set EVALH_TEST_REDIS_URL=redis://localhost:6379/0 "
            "(or your broker), or exclude `-m celery`."
        )
    if not _broker_reachable(url):
        pytest.fail(
            f"EVALH_TEST_REDIS_URL={url!r} is set but the broker is "
            "unreachable. Start the broker or unset the env var to skip."
        )
    _install_stub_agent()
    case_ids = ["c1", "c2"]
    plan = _build_plan(case_ids)
    cells = [_make_cell(c) for c in case_ids]

    executor = CeleryExecutor(broker_url=url, timeout=30)
    async with executor:
        await executor.open(plan)
        # Worker boots against the same in-process app — the task is
        # registered there via the executor's _build_celery_app.
        worker_thread = _start_inproc_worker(executor._celery_app)
        # Give the consumer a moment to declare its queue before we
        # publish; Redis is fast but not instant.
        time.sleep(0.5)
        try:
            executor.bind_cells(
                [
                    (cell, case, plan.variants[0])
                    for cell, case in zip(cells, plan.cases, strict=True)
                ]
            )
            outcomes = await executor.dispatch_all(cells)
            summary = executor.finalize()
        finally:
            # Best-effort shutdown — the daemon thread will die with the
            # test process anyway, but `broadcast` releases the queue
            # cleanly on a reused broker.
            with contextlib.suppress(Exception):
                executor._celery_app.control.shutdown()
            worker_thread.join(timeout=5)

    assert {o.case.id for o in outcomes} == set(case_ids)
    for o, case_id in zip(outcomes, case_ids, strict=True):
        assert o.trace.output.final_answer == f"ray-answer-for-{case_id}"
    assert summary.cases_total == 2
    assert summary.variants[0].name == "stub"


@pytest.mark.celery
async def test_celery_worker_rebuilds_evaluators_from_entry_points() -> None:
    """Companion to the Ray worker-rebuild test: ship a cell that names
    a built-in evaluator (``contains_text``) by string, run it through a
    real Celery worker, assert the worker resolved the name from the
    entry-point registry on its own end."""
    url = _redis_url()
    if url is None:
        pytest.fail(
            "EVALH_TEST_REDIS_URL is unset but @pytest.mark.celery was selected."
        )
    if not _broker_reachable(url):
        pytest.fail(
            f"EVALH_TEST_REDIS_URL={url!r} is set but broker unreachable."
        )
    _install_stub_agent()
    cell = _make_cell("c_rebuild_eval")
    cell.eval_config_dict["evaluators"] = [
        {
            "type": "contains_text",
            "name": "answer_present",
            "config": {
                "field": "output.final_answer",
                "all_of": ["answer-for-c_rebuild_eval"],
            },
        }
    ]

    plan = _build_plan(["c_rebuild_eval"])
    executor = CeleryExecutor(broker_url=url, timeout=30)
    async with executor:
        await executor.open(plan)
        worker_thread = _start_inproc_worker(executor._celery_app)
        time.sleep(0.5)
        try:
            executor.bind_cells([(cell, plan.cases[0], plan.variants[0])])
            outcomes = await executor.dispatch_all([cell])
        finally:
            with contextlib.suppress(Exception):
                executor._celery_app.control.shutdown()
            worker_thread.join(timeout=5)

    assert len(outcomes[0].results) == 1
    result = outcomes[0].results[0]
    assert result.evaluator == "answer_present"
    assert result.evaluator_type == "contains_text"
    assert result.passed is True
