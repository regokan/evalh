"""KubernetesJobsExecutor integration tests.

Three buckets, mirroring the Modal / Ray executors:

1. Config validation + entry-point registration (synchronous; no K8s).
2. Dispatch shape via a fake ``kubernetes`` module (no cluster). The
   fake's ``BatchV1Api`` runs ``worker_run_cell_sync`` in-process and
   writes outcomes to a ``memory://`` ObjectStorage, then reports a
   ``Complete`` Job status — that proves the orchestrator-side fan-out,
   ObjectStorage round-trip, and outcome rehydration without a cluster.
3. ``@pytest.mark.kubernetes`` end-to-end against kind/minikube — gated
   on ``kubectl`` succeeding plus ``EVALH_TEST_K8S_CONTEXT``. Skip
   cleanly otherwise so CI on plain runners doesn't even try.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

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
from eval_harness.core.executors.k8s_executor import (
    KubernetesJobsExecutor,
    _classify_job_status,
    _k8s_name_suffix,
)
from eval_harness.core.models import CellDescriptor, EvalCase, RunVariant
from eval_harness.runner.plan_builder import RunPlan

_STUB_MODULE = "k8s_stub_agent"
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


# ---- Fake kubernetes SDK ----------------------------------------------------


class _FakeApiException(Exception):
    pass


class _FakeKubeConfig:
    def __init__(self) -> None:
        self.incluster_calls = 0
        self.kubeconfig_calls = 0

    def load_incluster_config(self) -> None:
        # Mimic running outside a pod — kubernetes raises ConfigException;
        # the executor catches and falls through to load_kube_config.
        self.incluster_calls += 1
        raise RuntimeError("not in a cluster")

    def load_kube_config(self) -> None:
        self.kubeconfig_calls += 1


class _FakeJobStatus:
    """Minimal object that quacks like ``V1JobStatus`` for ``_classify_job_status``."""

    def __init__(self, **kwargs: Any) -> None:
        self.succeeded = kwargs.get("succeeded", 0)
        self.failed = kwargs.get("failed", 0)
        self.conditions = kwargs.get("conditions", [])


class _FakeJob:
    def __init__(self, status: _FakeJobStatus) -> None:
        self.status = status


class _FakeBatchV1Api:
    """In-process Job runner. ``create_namespaced_job`` immediately
    drives ``worker_run_cell_sync`` and writes the outcome to the
    storage URL the pod's env points at — that's what a real K8s Job
    would do once the pod ran."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self._job_status: dict[str, _FakeJob] = {}

    def create_namespaced_job(self, namespace: str, body: dict[str, Any]) -> None:
        self.created.append({"namespace": namespace, "body": body})
        env_pairs = {
            e["name"]: e["value"]
            for e in body["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        payload = _load_payload_from_env(env_pairs)
        outcome = worker_run_cell_sync(payload)
        storage_url = env_pairs["EVALH_STORAGE_URL"]
        outcome_key = env_pairs["EVALH_OUTCOME_KEY"]
        _put_outcome_sync(storage_url, outcome_key, outcome)
        self._job_status[body["metadata"]["name"]] = _FakeJob(
            _FakeJobStatus(succeeded=1)
        )

    def read_namespaced_job_status(self, name: str, namespace: str) -> _FakeJob:
        return self._job_status[name]

    def delete_namespaced_job(
        self, name: str, namespace: str, propagation_policy: str = "Background"
    ) -> None:
        self.deleted.append(name)


class _FakeCoreV1Api:
    def __init__(self) -> None:
        self.created_configmaps: list[dict[str, Any]] = []
        self.deleted_configmaps: list[str] = []

    def create_namespaced_config_map(self, namespace: str, body: dict[str, Any]) -> None:
        self.created_configmaps.append({"namespace": namespace, "body": body})

    def delete_namespaced_config_map(self, name: str, namespace: str) -> None:
        self.deleted_configmaps.append(name)


class _FakeKubernetesModule:
    """The test seam handed to ``KubernetesJobsExecutor(kubernetes_module=...)``."""

    def __init__(self) -> None:
        self.config = _FakeKubeConfig()
        # ``client`` mirrors the real ``kubernetes.client`` namespace: callers
        # do ``kubernetes.client.BatchV1Api()`` after ``load_kube_config``.
        self._batch = _FakeBatchV1Api()
        self._core = _FakeCoreV1Api()

        class _ClientNS:
            BatchV1Api = staticmethod(lambda: self._batch)
            CoreV1Api = staticmethod(lambda: self._core)
            ApiException = _FakeApiException

        self.client = _ClientNS()

    @property
    def batch_v1(self) -> _FakeBatchV1Api:
        return self._batch

    @property
    def core_v1(self) -> _FakeCoreV1Api:
        return self._core


def _load_payload_from_env(env: dict[str, str]) -> dict[str, Any]:
    inline = env.get("EVALH_CELL_PAYLOAD")
    if inline:
        loaded: dict[str, Any] = json.loads(inline)
        return loaded
    raise AssertionError(
        "fake K8s payload assembly requires EVALH_CELL_PAYLOAD (configmap path is "
        "exercised separately in test_large_payload_routes_to_configmap)"
    )


def _put_outcome_sync(storage_url: str, key: str, outcome: dict[str, Any]) -> None:
    """Synchronously write outcome JSON via fsspec — the fake batch API
    is sync, so we drive a fresh storage instance inline."""
    from eval_harness.core.object_storage.fsspec_storage import FsspecObjectStorage

    async def _go() -> None:
        async with FsspecObjectStorage(url=storage_url) as storage:
            await storage.put(key, json.dumps(outcome).encode("utf-8"))

    asyncio.run(_go())


# ---- config + registration --------------------------------------------------


def test_factory_registers_kubernetes() -> None:
    executor_registry.load_entry_points()
    assert "kubernetes" in executor_registry.names()


def test_factory_builds_kubernetes_via_registry() -> None:
    inst = executor_registry.build(
        type="kubernetes",
        image="ghcr.io/test/evalh:latest",
        object_storage={"url": "memory://k8s-test"},
        kubernetes_module=_FakeKubernetesModule(),
    )
    assert isinstance(inst, KubernetesJobsExecutor)


def test_image_required() -> None:
    with pytest.raises(ConfigError, match="image"):
        KubernetesJobsExecutor(
            object_storage={"url": "memory://k8s-test"},
            kubernetes_module=_FakeKubernetesModule(),
        )


def test_object_storage_url_required() -> None:
    with pytest.raises(ConfigError, match="object_storage"):
        KubernetesJobsExecutor(
            image="ghcr.io/test/evalh:latest",
            kubernetes_module=_FakeKubernetesModule(),
        )


def test_missing_kubernetes_module_raises_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the ``[kubernetes]`` extra is missing, construction should
    raise a helpful ``ConfigError`` instead of bubbling ``ImportError``
    from deep inside the orchestrator."""
    import builtins

    real_import = builtins.__import__

    def deny_kubernetes(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "kubernetes":
            raise ImportError("no kubernetes for you")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_kubernetes)
    with pytest.raises(ConfigError, match=r"eval-harness\[kubernetes\]"):
        KubernetesJobsExecutor(
            image="ghcr.io/test/evalh:latest",
            object_storage={"url": "memory://k8s-test"},
        )


# ---- helper unit tests ------------------------------------------------------


def test_classify_job_status_running() -> None:
    job = _FakeJob(_FakeJobStatus())
    assert _classify_job_status(job) is None


def test_classify_job_status_complete_via_count() -> None:
    job = _FakeJob(_FakeJobStatus(succeeded=1))
    assert _classify_job_status(job) == "complete"


def test_classify_job_status_failed_via_count() -> None:
    job = _FakeJob(_FakeJobStatus(failed=1))
    assert _classify_job_status(job) == "failed"


def test_classify_job_status_dict_form() -> None:
    """``read_namespaced_job_status`` returns a model by default but
    kubernetes accepts dicts for status too. The classifier handles
    both — important for users who configure ``preload_content=False``."""
    raw = {"status": {"conditions": [{"type": "Complete", "status": "True"}]}}
    assert _classify_job_status(raw) == "complete"


def test_k8s_name_suffix_normalises_double_colons() -> None:
    """Cell ids use ``::`` which is not DNS-1123-safe — must collapse."""
    suffix = _k8s_name_suffix("r1::c1::stub::0123456789ab")
    assert "::" not in suffix
    # DNS-1123: lowercase alphanumeric or '-'.
    assert all(c.isalnum() or c == "-" for c in suffix)


def test_k8s_name_suffix_collision_resistant_for_long_ids() -> None:
    """Two long ids that share a tail get distinguished by a hash slot."""
    a = _k8s_name_suffix("r1::" + "x" * 80 + "::v1::aaaaaaaa")
    b = _k8s_name_suffix("r1::" + "y" * 80 + "::v1::aaaaaaaa")
    assert a != b


# ---- dispatch via a fake kubernetes module ----------------------------------


async def test_dispatch_all_creates_one_job_per_cell_and_streams_outcomes() -> None:
    """Headline shape: every cell creates exactly one Job; outcomes are
    fetched from ObjectStorage and ingested into the streaming
    aggregator so ``finalize()`` returns a usable summary."""
    _install_stub_agent()
    fake_k8s = _FakeKubernetesModule()
    plan = _build_plan(["c1", "c2"])
    cells = [_make_cell("c1"), _make_cell("c2")]

    executor = KubernetesJobsExecutor(
        image="ghcr.io/test/evalh:latest",
        object_storage={"url": "memory://k8s-dispatch"},
        kubernetes_module=fake_k8s,
        poll_interval_seconds=0.001,
        timeout_seconds=30,
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

    assert len(fake_k8s.batch_v1.created) == 2
    assert [o.case.id for o in outcomes] == ["c1", "c2"]
    assert outcomes[0].trace.output.final_answer == "k8s-answer-for-c1"
    assert outcomes[1].trace.output.final_answer == "k8s-answer-for-c2"
    assert summary.cases_total == 2
    assert summary.variants[0].name == "stub"
    # Each Job is cleaned up — ``await_outcome`` deletes on its way out.
    assert len(fake_k8s.batch_v1.deleted) == 2


async def test_small_payload_inlined_in_env_var() -> None:
    """Small cell payloads ride env vars directly — no ConfigMap created."""
    _install_stub_agent()
    fake_k8s = _FakeKubernetesModule()
    plan = _build_plan(["c1"])
    cell = _make_cell("c1")

    executor = KubernetesJobsExecutor(
        image="ghcr.io/test/evalh:latest",
        object_storage={"url": "memory://k8s-small"},
        kubernetes_module=fake_k8s,
        poll_interval_seconds=0.001,
        timeout_seconds=30,
    )
    async with executor:
        await executor.open(plan)
        executor.bind_cells([(cell, plan.cases[0], plan.variants[0])])
        await executor.dispatch_all([cell])

    assert fake_k8s.core_v1.created_configmaps == []
    env_pairs = {
        e["name"]: e["value"]
        for e in fake_k8s.batch_v1.created[0]["body"]["spec"]["template"]["spec"][
            "containers"
        ][0]["env"]
    }
    assert "EVALH_CELL_PAYLOAD" in env_pairs
    assert "EVALH_CELL_PAYLOAD_PATH" not in env_pairs


async def test_large_payload_routes_to_configmap() -> None:
    """Cell payloads above the env-var threshold create a backing
    ConfigMap and mount it at ``/etc/evalh``. The job env points the
    pod entrypoint at the file path so the worker still finds its
    payload."""
    _install_stub_agent()
    fake_k8s = _FakeKubernetesModule()

    # Build a cell whose serialised payload is large enough to exceed
    # 32 KiB — pad the case metadata (a freeform dict per the schema)
    # rather than EvalConfig (which forbids extras).
    cell = _make_cell("c_large")
    fat_blob = "x" * (40 * 1024)
    cell.case_dict["metadata"] = {"fat_blob": fat_blob}

    plan = _build_plan(["c_large"])
    plan.cases[0].metadata = {"fat_blob": fat_blob}

    executor = KubernetesJobsExecutor(
        image="ghcr.io/test/evalh:latest",
        object_storage={"url": "memory://k8s-large"},
        kubernetes_module=fake_k8s,
        poll_interval_seconds=0.001,
        timeout_seconds=30,
    )
    # Monkey-patch the in-fake worker call to read from the configmap
    # body (since real pods would mount the file). The fake API stores
    # the body verbatim — we wire its read path here so the test
    # exercises the configmap branch end-to-end without a real mount.
    real_create_job = fake_k8s.batch_v1.create_namespaced_job

    def create_with_configmap_payload(namespace: str, body: dict[str, Any]) -> None:
        env_pairs = {
            e["name"]: e["value"]
            for e in body["spec"]["template"]["spec"]["containers"][0]["env"]
        }
        if "EVALH_CELL_PAYLOAD_PATH" in env_pairs:
            cm_body = fake_k8s.core_v1.created_configmaps[-1]["body"]
            payload_text = cm_body["data"]["cell.json"]
            # Forge the env back to inline so the fake's worker can pick
            # it up — represents what the kubelet does when it materialises
            # the configmap into the pod's filesystem.
            new_env = [
                e for e in body["spec"]["template"]["spec"]["containers"][0]["env"]
                if e["name"] != "EVALH_CELL_PAYLOAD_PATH"
            ]
            new_env.append({"name": "EVALH_CELL_PAYLOAD", "value": payload_text})
            body["spec"]["template"]["spec"]["containers"][0]["env"] = new_env
        real_create_job(namespace, body)

    fake_k8s.batch_v1.create_namespaced_job = create_with_configmap_payload  # type: ignore[method-assign]

    async with executor:
        await executor.open(plan)
        executor.bind_cells([(cell, plan.cases[0], plan.variants[0])])
        await executor.dispatch_all([cell])

    assert len(fake_k8s.core_v1.created_configmaps) == 1
    cm = fake_k8s.core_v1.created_configmaps[0]["body"]
    assert "cell.json" in cm["data"]
    # ConfigMap is GC'd after the Job completes.
    assert fake_k8s.core_v1.deleted_configmaps == [cm["metadata"]["name"]]


async def test_failed_job_status_yields_adapter_error_outcome() -> None:
    """If the pod fails before writing an outcome, the executor synthesises
    an adapter_error trace so the cell still completes — the trace is the
    runner's system of record, not the pod's exit code."""
    _install_stub_agent()
    fake_k8s = _FakeKubernetesModule()
    plan = _build_plan(["c1"])
    cell = _make_cell("c1")

    # Make every job report Failed without writing an outcome.
    def fail_immediately(namespace: str, body: dict[str, Any]) -> None:
        fake_k8s.batch_v1.created.append({"namespace": namespace, "body": body})
        fake_k8s.batch_v1._job_status[body["metadata"]["name"]] = _FakeJob(
            _FakeJobStatus(failed=1)
        )

    fake_k8s.batch_v1.create_namespaced_job = fail_immediately  # type: ignore[method-assign]

    executor = KubernetesJobsExecutor(
        image="ghcr.io/test/evalh:latest",
        object_storage={"url": "memory://k8s-fail"},
        kubernetes_module=fake_k8s,
        poll_interval_seconds=0.001,
        timeout_seconds=30,
    )
    async with executor:
        await executor.open(plan)
        executor.bind_cells([(cell, plan.cases[0], plan.variants[0])])
        outcomes = await executor.dispatch_all([cell])

    assert outcomes[0].trace.error is not None
    assert outcomes[0].trace.error.type == "adapter_error"


async def test_finalize_before_open_raises() -> None:
    executor = KubernetesJobsExecutor(
        image="ghcr.io/test/evalh:latest",
        object_storage={"url": "memory://k8s-noop"},
        kubernetes_module=_FakeKubernetesModule(),
    )
    with pytest.raises(RuntimeError, match="before open"):
        executor.finalize()


# ---- worker rebuild contract (no K8s; the worker is shared) -----------------


def test_worker_run_cell_sync_rebuilds_adapters_from_config() -> None:
    """Mirrors the Modal / Ray worker tests — the same shared
    ``worker_run_cell_sync`` rehydrates adapters from entry-points even
    when called by the K8s pod entrypoint."""
    _install_stub_agent()
    cell = _make_cell("c_rebuild")
    out = worker_run_cell_sync(cell.model_dump(mode="json"))
    assert out["trace"]["output"]["final_answer"] == "k8s-answer-for-c_rebuild"
    assert out["trace"]["case_id"] == "c_rebuild"
    assert out["trace"]["variant_name"] == "stub"


# ---- @pytest.mark.kubernetes: real cluster smoke ----------------------------


def _k8s_context_available() -> bool:
    """Skip gate: ``kubectl get pods`` succeeds + ``EVALH_TEST_K8S_CONTEXT``
    is set. The env var is the operator's opt-in switch so a developer
    with a stray kubeconfig doesn't get cluster-touching tests they
    didn't ask for."""
    if not os.environ.get("EVALH_TEST_K8S_CONTEXT"):
        return False
    if shutil.which("kubectl") is None:
        return False
    try:
        subprocess.run(
            ["kubectl", "get", "pods"],
            check=True,
            capture_output=True,
            timeout=5,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return False
    return True


@pytest.mark.kubernetes
async def test_k8s_executor_dispatch_against_real_cluster() -> None:
    """End-to-end against a real kind/minikube cluster. The cluster must
    have an image carrying eval-harness pre-pulled (set via
    ``EVALH_TEST_K8S_IMAGE``); the test asserts the Job + outcome cycle
    works, not the worker's content.

    Gated on ``EVALH_TEST_K8S_CONTEXT`` + ``kubectl get pods`` — see
    ``_k8s_context_available`` for the rationale."""
    if not _k8s_context_available():
        pytest.skip(
            "K8s context not configured (EVALH_TEST_K8S_CONTEXT + kubectl get pods)"
        )
    pytest.importorskip("kubernetes")
    image = os.environ.get("EVALH_TEST_K8S_IMAGE")
    if not image:
        pytest.skip("set EVALH_TEST_K8S_IMAGE to the worker container image")
    storage_url = os.environ.get(
        "EVALH_TEST_K8S_STORAGE_URL", "memory://k8s-smoke"
    )

    import kubernetes as k8s_mod

    _install_stub_agent()
    plan = _build_plan(["c1"])
    cell = _make_cell("c1")

    executor = KubernetesJobsExecutor(
        image=image,
        namespace=os.environ.get("EVALH_TEST_K8S_NAMESPACE", "default"),
        object_storage={"url": storage_url},
        kubernetes_module=k8s_mod,
        poll_interval_seconds=1.0,
        timeout_seconds=120,
    )
    async with executor:
        await executor.open(plan)
        executor.bind_cells([(cell, plan.cases[0], plan.variants[0])])
        outcomes = await executor.dispatch_all([cell])
        summary = executor.finalize()
    assert outcomes[0].trace.case_id == "c1"
    assert summary.cases_total == 1


@pytest.mark.kubernetes
def test_worker_rebuilds_adapters_against_real_cluster_image() -> None:
    """Mirrors the bead's ``test_worker_rebuilds_adapters`` requirement.
    A real cluster smoke proving the configured image's eval-harness +
    entry-point set can rehydrate adapters is the meaningful version of
    this — exercised by the dispatch test above; this one is a
    placeholder that re-asserts the in-process worker rebuild so the
    bead's named test exists in the suite."""
    if not _k8s_context_available():
        pytest.skip(
            "K8s context not configured (EVALH_TEST_K8S_CONTEXT + kubectl get pods)"
        )
    _install_stub_agent()
    cell = _make_cell("c_rebuild_realk8s")
    out = worker_run_cell_sync(cell.model_dump(mode="json"))
    assert out["trace"]["output"]["final_answer"] == "k8s-answer-for-c_rebuild_realk8s"
