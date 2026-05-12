"""RayExecutor integration tests.

Three buckets:

1. Config validation + entry-point registration (synchronous; no Ray
   involvement).
2. Dispatch shape via a fake ``ray`` module (no Ray runtime). Proves the
   orchestrator-side fan-out + outcome rehydration without paying the
   ``ray.init`` startup tax.
3. ``@pytest.mark.ray`` end-to-end against Ray's local cluster mode — the
   single integration test the bead calls for. Gate: skip cleanly when
   ``[ray]`` extra isn't installed, fail loudly if it is but
   ``ray.init`` errors (mirrors the docker_volume pattern from v1).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

ray = pytest.importorskip("ray")

from eval_harness.core.config import (  # noqa: E402
    DatasetConfig,
    EvalConfig,
    EvalIdentity,
    OutputConfig,
    SystemConfig,
)
from eval_harness.core.errors import ConfigError  # noqa: E402
from eval_harness.core.executors._worker import worker_run_cell_sync  # noqa: E402
from eval_harness.core.executors.base import executor_registry  # noqa: E402
from eval_harness.core.executors.ray_executor import RayExecutor  # noqa: E402
from eval_harness.core.models import CellDescriptor, EvalCase, RunVariant  # noqa: E402
from eval_harness.runner.plan_builder import RunPlan  # noqa: E402

# Real, importable stub agent module. Lives at ``tests/fixtures/ray_stub_agent.py``
# so Ray workers (which run in subprocesses with their own ``sys.modules``)
# can ``import`` it after the test ships ``tests/fixtures`` via
# ``runtime_env={"working_dir": ...}``. In-process tests (the fake-ray
# bucket + the worker_run_cell_sync bucket) add the same directory to
# ``sys.path`` so the python_function adapter resolves the target the
# same way the Ray worker would.
_STUB_MODULE = "ray_stub_agent"
_STUB_DIR = Path(__file__).resolve().parents[1] / "fixtures"


def _install_stub_agent() -> None:
    """Ensure the stub agent module is resolvable in *this* process by
    adding its directory to ``sys.path``. The Ray worker process bucket
    relies on ``runtime_env={"working_dir": ...}`` instead."""
    stub_dir = str(_STUB_DIR)
    if stub_dir not in sys.path:
        sys.path.insert(0, stub_dir)
    # Drop a stale module cache from a prior test run so the import
    # picks up the freshly-pathed copy.
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


def test_factory_registers_ray() -> None:
    executor_registry.load_entry_points()
    assert "ray" in executor_registry.names()


def test_factory_builds_ray_via_registry() -> None:
    inst = executor_registry.build(type="ray")
    assert isinstance(inst, RayExecutor)


def test_missing_ray_module_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the ``[ray]`` extra is missing, construction should raise a
    helpful ``ConfigError`` instead of a raw ``ImportError`` deep inside
    the orchestrator."""
    import builtins

    real_import = builtins.__import__

    def deny_ray(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "ray":
            raise ImportError("no ray for you")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_ray)
    with pytest.raises(ConfigError, match=r"eval-harness\[ray\]"):
        RayExecutor()


def test_num_cpus_and_gpus_forwarded_to_remote() -> None:
    """The ``num_cpus_per_cell`` / ``num_gpus_per_cell`` knobs reach the
    ``ray.remote(...)`` call the executor builds at ``open(plan)``. We
    capture them via a fake ray module rather than spinning up a cluster."""

    captured_remote_kwargs: dict[str, Any] = {}

    def fake_remote(**kwargs: Any) -> Any:
        captured_remote_kwargs.update(kwargs)

        def _wrap(fn: Any) -> Any:
            return _FakeRemoteFunction(fn)

        return _wrap

    fake_ray = _FakeRayModule(remote_impl=fake_remote)
    exec_ = RayExecutor(
        ray_module=fake_ray,
        num_cpus_per_cell=4,
        num_gpus_per_cell=0.5,
    )

    plan = _build_plan(["c1"])

    async def _drive() -> None:
        async with exec_:
            await exec_.open(plan)

    import asyncio as _asyncio

    _asyncio.run(_drive())

    assert captured_remote_kwargs == {"num_cpus": 4, "num_gpus": 0.5}
    assert fake_ray.init_calls == 1


def test_object_store_memory_default_is_forwarded_to_ray_init() -> None:
    """GHA's ``/dev/shm`` defaults to ~64 MiB; Ray's auto-sized 2 GiB
    plasma store crashes ``ray.init`` there. The executor's default
    ``object_store_memory=78_643_200`` (~75 MiB) is the CI-safe value
    that fits inside GHA's shm; real-cluster runs override it."""
    fake_ray = _FakeRayModule()
    exec_ = RayExecutor(ray_module=fake_ray, address=None)
    plan = _build_plan(["c1"])

    import asyncio as _asyncio

    async def _drive() -> None:
        async with exec_:
            await exec_.open(plan)

    _asyncio.run(_drive())
    assert fake_ray.init_kwargs.get("object_store_memory") == 78_643_200


def test_object_store_memory_override_forwarded_to_ray_init() -> None:
    """Production callers override the CI default. ``None`` is the
    sentinel that means 'let Ray auto-size' — the executor must NOT
    forward ``object_store_memory`` to ``ray.init`` in that case."""
    fake_ray = _FakeRayModule()
    exec_ = RayExecutor(
        ray_module=fake_ray, address=None, object_store_memory=4_000_000_000
    )
    plan = _build_plan(["c1"])

    import asyncio as _asyncio

    async def _drive() -> None:
        async with exec_:
            await exec_.open(plan)

    _asyncio.run(_drive())
    assert fake_ray.init_kwargs.get("object_store_memory") == 4_000_000_000

    fake_ray2 = _FakeRayModule()
    exec2 = RayExecutor(
        ray_module=fake_ray2, address=None, object_store_memory=None
    )

    async def _drive2() -> None:
        async with exec2:
            await exec2.open(plan)

    _asyncio.run(_drive2())
    assert "object_store_memory" not in fake_ray2.init_kwargs


# ---- dispatch via a fake Ray module -----------------------------------------


class _FakeObjectRef:
    """Quacks like ``ray.ObjectRef`` — carries the worker's already-computed
    return value. ``ray.get`` reads ``.value``."""

    def __init__(self, value: Any) -> None:
        self.value = value


class _FakeRemoteFunction:
    """Mirrors ``ray.remote(fn)``: callable as ``fn.remote(...)``. Runs the
    underlying function synchronously in-process and stores the result in
    a ``_FakeObjectRef`` so ``ray.get`` can return it."""

    def __init__(self, fn: Any) -> None:
        self._fn = fn
        self.calls: list[Any] = []

    def remote(self, payload: dict[str, Any]) -> _FakeObjectRef:
        self.calls.append(payload)
        return _FakeObjectRef(self._fn(payload))


class _FakeRayModule:
    """Test seam — exposes the four methods the executor reaches for:
    ``init``, ``is_initialized``, ``remote``, ``get``, ``shutdown``."""

    def __init__(self, remote_impl: Any | None = None) -> None:
        self.init_calls = 0
        self.shutdown_calls = 0
        self.remote_calls: list[_FakeRemoteFunction] = []
        self._initialised = False
        self._remote_impl = remote_impl

    def init(self, **kwargs: Any) -> None:
        self.init_calls += 1
        self._initialised = True
        self.init_kwargs = kwargs

    def is_initialized(self) -> bool:
        return self._initialised

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        self._initialised = False

    def remote(self, **kwargs: Any) -> Any:
        if self._remote_impl is not None:
            return self._remote_impl(**kwargs)

        def _wrap(fn: Any) -> Any:
            rf = _FakeRemoteFunction(fn)
            self.remote_calls.append(rf)
            return rf

        return _wrap

    def get(self, handle: _FakeObjectRef) -> Any:
        return handle.value


async def test_dispatch_all_submits_one_task_per_cell_and_streams_outcomes() -> None:
    """Headline end-to-end shape: every cell becomes one Ray task; outcomes
    are rehydrated through the bound (case, variant) index and ingested
    into the streaming aggregator so ``finalize()`` returns a summary."""
    _install_stub_agent()
    fake_ray = _FakeRayModule()

    plan = _build_plan(["c1", "c2"])
    cells = [_make_cell("c1"), _make_cell("c2")]

    executor = RayExecutor(ray_module=fake_ray)
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

    # One ``ray.remote`` decoration; two ``.remote(...)`` calls — the
    # decorator is built once at open(plan), then re-used per cell.
    assert len(fake_ray.remote_calls) == 1
    rf = fake_ray.remote_calls[0]
    assert len(rf.calls) == 2
    # Each payload carries a populated `case_dict` / `eval_config_dict`
    # (the executor fills them from the bound (case, variant) before
    # sending — the runner leaves them empty by design). The two
    # submit_cell calls run via asyncio.gather across thread pool
    # workers so spawn order isn't deterministic; assert the *set*.
    submitted_ids = {p["case_dict"]["id"] for p in rf.calls}
    assert submitted_ids == {"c1", "c2"}
    for payload in rf.calls:
        assert payload["eval_config_dict"]["systems"][0]["adapter"] == "python_function"

    assert [o.case.id for o in outcomes] == ["c1", "c2"]
    assert outcomes[0].trace.output.final_answer == "ray-answer-for-c1"
    assert summary.cases_total == 2
    assert summary.variants[0].name == "stub"


async def test_finalize_before_open_raises() -> None:
    executor = RayExecutor(ray_module=_FakeRayModule())
    with pytest.raises(RuntimeError, match="before open"):
        executor.finalize()


async def test_close_shuts_down_ray_when_we_initialised_it() -> None:
    """Lifecycle contract: if we called ``ray.init`` (the cluster wasn't
    already up), ``__aexit__`` must call ``ray.shutdown``. Callers who
    pre-initialised Ray themselves keep their cluster alive."""
    fake_ray = _FakeRayModule()
    plan = _build_plan(["c1"])
    executor = RayExecutor(ray_module=fake_ray)
    async with executor:
        await executor.open(plan)
        assert fake_ray.init_calls == 1
    assert fake_ray.shutdown_calls == 1


async def test_close_skips_shutdown_when_caller_already_initialised_ray() -> None:
    fake_ray = _FakeRayModule()
    fake_ray._initialised = True  # caller has their own cluster up
    plan = _build_plan(["c1"])
    executor = RayExecutor(ray_module=fake_ray)
    async with executor:
        await executor.open(plan)
        assert fake_ray.init_calls == 0
    assert fake_ray.shutdown_calls == 0


# ---- worker rebuild contract (no Ray; the worker is shared) ----------------


def test_worker_run_cell_sync_rebuilds_adapters_from_config() -> None:
    """The Ray worker entry point is the same ``worker_run_cell_sync``
    Modal calls. Workers receive *only* config + case dict — they rebuild
    adapters via the entry-point registry. Exercising the sync path here
    proves the same contract the bead names as the Ray worker contract."""
    _install_stub_agent()
    cell = _make_cell("c_rebuild")
    out = worker_run_cell_sync(cell.model_dump(mode="json"))
    assert out["trace"]["output"]["final_answer"] == "ray-answer-for-c_rebuild"
    # No adapter instance was shipped — the worker resolved
    # `python_function` from the entry-point registry on its own.
    assert out["trace"]["case_id"] == "c_rebuild"
    assert out["trace"]["variant_name"] == "stub"


# ---- @pytest.mark.ray: real local-cluster smoke -----------------------------


_RAY_INIT_ERROR: str | None = None


def _ray_init_works() -> bool:
    """Probe ``ray.init`` once. Captures the failure reason into
    ``_RAY_INIT_ERROR`` so the @pytest.mark.ray gate surfaces it in
    the test output — swallowing the exception silently masks real
    bugs (ev-bkz).

    ``runtime_env.working_dir`` ships the stub-agent fixture directory
    to workers so the python_function adapter's ``module:func`` target
    resolves in the worker subprocess. eval-harness itself is pip-
    installed so workers find it through the inherited site-packages."""
    global _RAY_INIT_ERROR
    try:
        if not ray.is_initialized():
            ray.init(
                num_cpus=2,
                include_dashboard=False,
                log_to_driver=False,
                ignore_reinit_error=True,
                configure_logging=False,
                runtime_env={"working_dir": str(_STUB_DIR)},
                # Mirror the RayExecutor default — GHA's /dev/shm is ~64 MiB
                # so Ray's auto-sized 2 GiB plasma store crashes the boot.
                object_store_memory=78_643_200,
            )
        return True
    except Exception as e:
        _RAY_INIT_ERROR = f"{type(e).__name__}: {e}"
        return False


@pytest.mark.ray
async def test_ray_executor_dispatch_end_to_end_via_local_ray() -> None:
    """End-to-end: spin up Ray's local cluster, dispatch a handful of
    cells through ``RayExecutor``, verify outcomes match the worker's
    output and the aggregator finalises. Proves that the executor's
    contract works with the real Ray runtime, not just the test seam."""
    if not _ray_init_works():
        pytest.fail(
            "ray installed but ray.init() failed: "
            f"{_RAY_INIT_ERROR}. Mirrors the docker_volume v1 gate: fail "
            "loudly when the runtime is broken so we don't false-green "
            "on a real bug."
        )
    _install_stub_agent()
    case_ids = [f"c{i}" for i in range(10)]
    plan = _build_plan(case_ids)
    cells = [_make_cell(c) for c in case_ids]
    try:
        executor = RayExecutor(ray_module=ray, address=None)
        async with executor:
            await executor.open(plan)
            executor.bind_cells(
                [(cell, case, plan.variants[0]) for cell, case in zip(cells, plan.cases, strict=True)]
            )
            outcomes = await executor.dispatch_all(cells)
            summary = executor.finalize()
        # Match the streaming-summary discipline: every cell shows up.
        assert {o.case.id for o in outcomes} == set(case_ids)
        for o, case_id in zip(outcomes, case_ids, strict=True):
            assert o.trace.output.final_answer == f"ray-answer-for-{case_id}"
        assert summary.cases_total == 10
        assert summary.variants[0].name == "stub"
    finally:
        if ray.is_initialized():
            ray.shutdown()


@pytest.mark.ray
async def test_ray_worker_rebuilds_evaluators_from_entry_points() -> None:
    """The bead's headline rebuild test: ship a cell that names a
    built-in evaluator (``contains_text``) by string, run it through a
    real Ray worker, assert the worker resolved the name from the
    entry-point registry on its own end."""
    if not _ray_init_works():
        pytest.fail(
            f"ray installed but ray.init() failed: {_RAY_INIT_ERROR}; "
            "see test_ray_executor_dispatch_end_to_end_via_local_ray "
            "for details."
        )
    _install_stub_agent()
    # Cell config wires a `contains_text` evaluator — the worker has to
    # build it from the entry-point registry. The stub agent always
    # returns "ray-answer-for-<id>"; we assert the contains_text rule
    # passes for an "answer-for" substring.
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
    try:
        executor = RayExecutor(ray_module=ray, address=None)
        async with executor:
            await executor.open(plan)
            executor.bind_cells([(cell, plan.cases[0], plan.variants[0])])
            outcomes = await executor.dispatch_all([cell])
        assert len(outcomes[0].results) == 1
        result = outcomes[0].results[0]
        assert result.evaluator == "answer_present"
        assert result.evaluator_type == "contains_text"
        assert result.passed is True
    finally:
        if ray.is_initialized():
            ray.shutdown()
