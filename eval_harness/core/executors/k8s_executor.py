"""KubernetesJobsExecutor — each cell launches a Kubernetes Job pod.

Highest startup cost of the distributed executors (image pull + pod
spin-up dominate fast cells) but the most production-realistic shape
for large eval shops that already run K8s. Workers reuse
``_worker.worker_run_cell_sync`` — config travels, code doesn't.

Result piping is via ObjectStorage (F3): the pod writes its outcome JSON
to a configured fsspec URL, and ``await_outcome`` polls the Job status
and then reads the outcome bytes back. Cluster-side IPC stays on the
``Job`` primitive; the orchestrator never streams logs.

Payload routing — env var for the small case, ConfigMap for the large
case — is decided by ``_PAYLOAD_ENV_THRESHOLD_BYTES``. Pod env limits
are an aggregate ~1 MB; 32 KB leaves a comfortable headroom for the
other env vars and avoids surprises with K8s admission controllers that
sometimes cap individual values lower.

See docs/Executors.md for the K8s deployment recipe (base image, RBAC,
ObjectStorage URL).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
from contextlib import AsyncExitStack
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from eval_harness.core.errors import ConfigError
from eval_harness.core.executors.base import warn_if_local_files_with_distributed
from eval_harness.core.models import (
    CellDescriptor,
    EvalCase,
    EvaluationResult,
    RunSummary,
    RunVariant,
    Trace,
)
from eval_harness.core.object_storage.fsspec_storage import FsspecObjectStorage
from eval_harness.core.price_tables import warn_default_table_in_use
from eval_harness.runner.cost_accumulator import CostAccumulator
from eval_harness.runner.summary import SummaryAggregator

if TYPE_CHECKING:
    from eval_harness.runner.plan_builder import RunPlan
    from eval_harness.runner.run_eval import CellOutcome


_PAYLOAD_ENV_THRESHOLD_BYTES = 32 * 1024
_DEFAULT_POLL_INTERVAL_SECONDS = 2.0
_DEFAULT_TIMEOUT_SECONDS = 1800
_K8S_NAME_RE = re.compile(r"[^a-z0-9-]+")


class _Handle:
    """Opaque per-submission record. Owned by the executor; the runner
    only ever passes it back to ``await_outcome``."""

    __slots__ = ("cell_id", "configmap_name", "job_name", "outcome_key")

    def __init__(
        self,
        *,
        cell_id: str,
        job_name: str,
        configmap_name: str | None,
        outcome_key: str,
    ) -> None:
        self.cell_id = cell_id
        self.job_name = job_name
        self.configmap_name = configmap_name
        self.outcome_key = outcome_key


class KubernetesJobsExecutor:
    """Dispatches each cell as a Kubernetes ``batch/v1`` Job.

    Config:

    - ``namespace`` (str, default ``"default"``): namespace the Jobs and
      backing ConfigMaps live in.
    - ``image`` (str, required): container image carrying eval-harness +
      the consumer's plugin packages. Workers ``import`` the same
      adapter / evaluator entry-points the orchestrator installed
      locally — the image is the channel for that contract.
    - ``service_account`` (str, optional): pod ``serviceAccountName``.
      When the cluster needs RBAC scoped to a particular SA for the
      worker (e.g. to read a Secret with API keys), set this.
    - ``resources`` (dict, optional): Kubernetes ``resources`` block,
      forwarded into the container spec unchanged.
    - ``object_storage`` (dict, required): ``{"url": ..., "credentials": ...}``
      describing where the pod writes its outcome JSON. The
      orchestrator reads it back from the same place; tests use
      ``memory://`` so no real cloud is involved.
    - ``image_pull_secrets`` (list[str], optional): pull-secret names.
    - ``poll_interval_seconds`` (float, default 2.0): how often the
      orchestrator polls Job status.
    - ``timeout_seconds`` (int, default 1800): per-cell wall-clock
      ceiling. The orchestrator stops polling once exceeded and reports
      a timeout error trace; the Job's ``activeDeadlineSeconds`` is
      set to the same value so the cluster reaps the pod.
    - ``ttl_seconds_after_finished`` (int, default 600): K8s
      ``ttlSecondsAfterFinished`` on the Job so the cluster GCs it.
    - ``kubernetes_module`` (Any, test seam): a stand-in for the real
      ``kubernetes`` SDK. Production callers leave this ``None``; tests
      inject a fake exposing ``config``, ``client``.
    """

    def __init__(
        self,
        *,
        namespace: str = "default",
        image: str | None = None,
        service_account: str | None = None,
        resources: dict[str, Any] | None = None,
        object_storage: dict[str, Any] | None = None,
        image_pull_secrets: list[str] | None = None,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
        timeout_seconds: int = _DEFAULT_TIMEOUT_SECONDS,
        ttl_seconds_after_finished: int = 600,
        kubernetes_module: Any | None = None,
        **_extra: Any,
    ) -> None:
        if not image:
            raise ConfigError(
                "kubernetes executor: 'image' (str) is required — workers "
                "need the same entry-point set the orchestrator has"
            )
        if not object_storage or not object_storage.get("url"):
            raise ConfigError(
                "kubernetes executor: 'object_storage.url' is required — "
                "pods write outcome JSON through ObjectStorage so the "
                "orchestrator can read it back"
            )
        if kubernetes_module is None:
            try:
                import kubernetes as _kubernetes
            except ImportError as e:
                raise ConfigError(
                    "kubernetes executor requires the `kubernetes` package. "
                    "Install with: pip install 'eval-harness[kubernetes]'"
                ) from e
            kubernetes_module = _kubernetes

        self._k8s: Any = kubernetes_module
        self._namespace = namespace
        self._image = image
        self._service_account = service_account
        self._resources = dict(resources or {})
        self._image_pull_secrets = list(image_pull_secrets or [])
        self._storage_config = dict(object_storage)
        self._poll_interval = float(poll_interval_seconds)
        self._timeout_seconds = int(timeout_seconds)
        self._ttl_seconds_after_finished = int(ttl_seconds_after_finished)

        # Per-run state, populated in ``open(plan)``.
        self._plan: RunPlan | None = None
        self._accumulator: CostAccumulator | None = None
        self._aggregator: SummaryAggregator | None = None
        self._sink_errors: list[dict[str, Any]] = []
        self._cell_index: dict[str, tuple[EvalCase, RunVariant]] = {}
        self._exit_stack: AsyncExitStack | None = None
        self._batch_v1: Any | None = None
        self._core_v1: Any | None = None
        self._storage: FsspecObjectStorage | None = None

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
        """Build per-run state. Loads kube config (in-cluster first,
        local kubeconfig fallback) and opens the ObjectStorage we'll
        read outcomes from."""
        warn_if_local_files_with_distributed(plan, "kubernetes executor")
        self._plan = plan
        self._accumulator = CostAccumulator()
        self._aggregator = SummaryAggregator(plan=plan)
        if plan.price_table is not None:
            warn_default_table_in_use(plan.price_table)

        await asyncio.to_thread(self._load_kube_config)
        self._batch_v1 = self._k8s.client.BatchV1Api()
        self._core_v1 = self._k8s.client.CoreV1Api()

        self._exit_stack = AsyncExitStack()
        self._storage = FsspecObjectStorage(
            url=self._storage_config["url"],
            credentials=self._storage_config.get("credentials"),
        )
        await self._exit_stack.enter_async_context(self._storage)

    def _load_kube_config(self) -> None:
        """Try in-cluster first, fall back to local kubeconfig. The pod
        case (in-cluster) is the production path; the local case keeps
        ``kubectl``-equivalent setup working for dev."""
        try:
            self._k8s.config.load_incluster_config()
            return
        except Exception:
            # ConfigException when run outside a pod. Fall through to
            # local kubeconfig — that path raises a clearer error if
            # ~/.kube/config is also unavailable.
            pass
        self._k8s.config.load_kube_config()

    def bind_cells(
        self,
        cells: list[tuple[CellDescriptor, EvalCase, RunVariant]],
    ) -> None:
        """Index ``(case, variant)`` by ``cell_id`` so dispatch can
        populate ``case_dict`` / ``eval_config_dict`` payloads and
        ``await_outcome`` can rehydrate without shipping them back."""
        for cell, case, variant in cells:
            self._cell_index[cell.cell_id] = (case, variant)

    # ---- dispatch ------------------------------------------------------

    async def submit_cell(self, cell: CellDescriptor) -> _Handle:
        """Create the Job (and a ConfigMap, if needed). Returns the
        ``_Handle`` the caller awaits later via ``await_outcome``."""
        payload = self._payload_for(cell)
        payload_bytes = json.dumps(payload).encode("utf-8")
        job_name = self._job_name_for(cell)
        outcome_key = self._outcome_key_for(cell)
        env, configmap_name, volumes, volume_mounts = self._build_payload_carriers(
            cell=cell,
            payload_bytes=payload_bytes,
            outcome_key=outcome_key,
        )
        manifest = self._build_job_manifest(
            job_name=job_name,
            env=env,
            volumes=volumes,
            volume_mounts=volume_mounts,
        )
        batch_v1 = self._require_batch_v1()
        core_v1 = self._require_core_v1()
        if configmap_name is not None:
            await asyncio.to_thread(
                core_v1.create_namespaced_config_map,
                namespace=self._namespace,
                body=self._build_configmap_manifest(
                    name=configmap_name, payload_bytes=payload_bytes
                ),
            )
        await asyncio.to_thread(
            batch_v1.create_namespaced_job,
            namespace=self._namespace,
            body=manifest,
        )
        return _Handle(
            cell_id=cell.cell_id,
            job_name=job_name,
            configmap_name=configmap_name,
            outcome_key=outcome_key,
        )

    async def dispatch_all(self, cells: list[CellDescriptor]) -> list[CellOutcome]:
        """Submit every cell, then gather outcomes in input order. K8s
        cluster capacity is the natural rate limiter; we don't add a
        per-executor semaphore on top."""
        handles = await asyncio.gather(*(self.submit_cell(c) for c in cells))
        outcomes = await self.await_all(handles)
        for cell, outcome in zip(cells, outcomes, strict=True):
            self._ingest(cell, outcome)
        return outcomes

    async def await_outcome(self, handle: _Handle) -> CellOutcome:
        """Poll Job status until terminal, then read the outcome from
        ObjectStorage. Cleans up the Job + ConfigMap after."""
        try:
            terminal = await self._poll_until_terminal(handle.job_name)
            if terminal == "timeout":
                outcome = self._build_timeout_outcome(handle.cell_id)
            elif terminal == "failed":
                # Pod failed before writing the outcome — synthesise an
                # adapter_error trace so the cell still completes. If the
                # pod wrote partial output, the read below picks it up
                # and overrides this.
                outcome = self._build_failure_outcome(handle.cell_id)
                payload = await self._read_outcome_if_present(handle.outcome_key)
                if payload is not None:
                    outcome = self._outcome_from_dict(payload)
            else:
                payload = await self._read_outcome_if_present(handle.outcome_key)
                if payload is None:
                    outcome = self._build_missing_outcome_failure(handle.cell_id)
                else:
                    outcome = self._outcome_from_dict(payload)
        finally:
            await self._cleanup_handle(handle)
        return outcome

    async def await_all(self, handles: list[_Handle]) -> list[CellOutcome]:
        return list(
            await asyncio.gather(*(self.await_outcome(h) for h in handles))
        )

    async def close(self) -> None:
        return None

    # ---- aggregation ---------------------------------------------------

    def finalize(self) -> RunSummary:
        if self._aggregator is None:
            raise RuntimeError("KubernetesJobsExecutor.finalize called before open()")
        summary = self._aggregator.finalize()
        summary.sink_errors = self._sink_errors
        return summary

    @property
    def sink_errors(self) -> list[dict[str, Any]]:
        return self._sink_errors

    # ---- internals -----------------------------------------------------

    def _ingest(self, cell: CellDescriptor, outcome: CellOutcome) -> None:
        if self._aggregator is None:
            raise RuntimeError("KubernetesJobsExecutor._ingest called before open()")
        self._aggregator.add(outcome)

    def _payload_for(self, cell: CellDescriptor) -> dict[str, Any]:
        """Mirror ``RayExecutor._payload_for``: populate the worker's
        rehydration dicts from the bound (case, variant) when the runner
        left them empty."""
        payload = cell.model_dump(mode="json")
        if cell.cell_id not in self._cell_index:
            # Test seam path: caller submitted a cell without binding.
            return payload
        case, _variant = self._cell_index[cell.cell_id]
        if not payload.get("case_dict"):
            payload["case_dict"] = case.model_dump(mode="json")
        if not payload.get("eval_config_dict") and self._plan is not None:
            payload["eval_config_dict"] = self._plan.config.model_dump(mode="json")
        return payload

    def _build_payload_carriers(
        self,
        *,
        cell: CellDescriptor,
        payload_bytes: bytes,
        outcome_key: str,
    ) -> tuple[list[dict[str, Any]], str | None, list[dict[str, Any]], list[dict[str, Any]]]:
        """Build the env vars + volumes the pod needs to find its payload
        and write its outcome. Switches to a ConfigMap when the payload
        is too large for env vars."""
        env: list[dict[str, Any]] = [
            {"name": "EVALH_STORAGE_URL", "value": self._storage_config["url"]},
            {"name": "EVALH_OUTCOME_KEY", "value": outcome_key},
            {"name": "EVALH_TIMEOUT_SECONDS", "value": str(self._timeout_seconds)},
        ]
        credentials = self._storage_config.get("credentials")
        if credentials:
            env.append(
                {
                    "name": "EVALH_STORAGE_CREDENTIALS_JSON",
                    "value": json.dumps(credentials),
                }
            )
        if len(payload_bytes) <= _PAYLOAD_ENV_THRESHOLD_BYTES:
            env.append(
                {
                    "name": "EVALH_CELL_PAYLOAD",
                    "value": payload_bytes.decode("utf-8"),
                }
            )
            return env, None, [], []
        configmap_name = self._configmap_name_for(cell)
        env.append(
            {
                "name": "EVALH_CELL_PAYLOAD_PATH",
                "value": "/etc/evalh/cell.json",
            }
        )
        volumes = [
            {
                "name": "evalh-cell-payload",
                "configMap": {"name": configmap_name},
            }
        ]
        volume_mounts = [
            {
                "name": "evalh-cell-payload",
                "mountPath": "/etc/evalh",
                "readOnly": True,
            }
        ]
        return env, configmap_name, volumes, volume_mounts

    def _build_job_manifest(
        self,
        *,
        job_name: str,
        env: list[dict[str, Any]],
        volumes: list[dict[str, Any]],
        volume_mounts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        container: dict[str, Any] = {
            "name": "evalh-worker",
            "image": self._image,
            "command": ["evalh-cell-worker"],
            "env": env,
        }
        if self._resources:
            container["resources"] = self._resources
        if volume_mounts:
            container["volumeMounts"] = volume_mounts
        pod_spec: dict[str, Any] = {
            "restartPolicy": "Never",
            "containers": [container],
        }
        if self._service_account is not None:
            pod_spec["serviceAccountName"] = self._service_account
        if self._image_pull_secrets:
            pod_spec["imagePullSecrets"] = [
                {"name": s} for s in self._image_pull_secrets
            ]
        if volumes:
            pod_spec["volumes"] = volumes
        return {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": job_name,
                "namespace": self._namespace,
                "labels": {"app.kubernetes.io/managed-by": "eval-harness"},
            },
            "spec": {
                "backoffLimit": 0,
                "ttlSecondsAfterFinished": self._ttl_seconds_after_finished,
                "activeDeadlineSeconds": self._timeout_seconds,
                "template": {
                    "metadata": {
                        "labels": {
                            "app.kubernetes.io/managed-by": "eval-harness",
                            "app.kubernetes.io/component": "cell-worker",
                        }
                    },
                    "spec": pod_spec,
                },
            },
        }

    def _build_configmap_manifest(
        self, *, name: str, payload_bytes: bytes
    ) -> dict[str, Any]:
        return {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {
                "name": name,
                "namespace": self._namespace,
                "labels": {"app.kubernetes.io/managed-by": "eval-harness"},
            },
            "data": {"cell.json": payload_bytes.decode("utf-8")},
        }

    async def _poll_until_terminal(self, job_name: str) -> str:
        """Return ``"complete"``, ``"failed"``, or ``"timeout"`` when the
        Job reaches a terminal state or the wall-clock budget runs out."""
        batch_v1 = self._require_batch_v1()
        deadline = asyncio.get_event_loop().time() + self._timeout_seconds
        while True:
            status = await asyncio.to_thread(
                batch_v1.read_namespaced_job_status,
                name=job_name,
                namespace=self._namespace,
            )
            terminal = _classify_job_status(status)
            if terminal is not None:
                return terminal
            if asyncio.get_event_loop().time() >= deadline:
                return "timeout"
            await asyncio.sleep(self._poll_interval)

    async def _read_outcome_if_present(self, key: str) -> dict[str, Any] | None:
        if self._storage is None:
            raise RuntimeError("KubernetesJobsExecutor: storage not opened")
        if not await self._storage.exists(key):
            return None
        raw = await self._storage.get(key)
        loaded: dict[str, Any] = json.loads(raw.decode("utf-8"))
        return loaded

    async def _cleanup_handle(self, handle: _Handle) -> None:
        """Best-effort delete the Job + any backing ConfigMap. We swallow
        errors here so a cleanup hiccup doesn't mask the real outcome."""
        batch_v1 = self._require_batch_v1()
        core_v1 = self._require_core_v1()
        with contextlib.suppress(Exception):
            await asyncio.to_thread(
                batch_v1.delete_namespaced_job,
                name=handle.job_name,
                namespace=self._namespace,
                propagation_policy="Background",
            )
        if handle.configmap_name is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(
                    core_v1.delete_namespaced_config_map,
                    name=handle.configmap_name,
                    namespace=self._namespace,
                )

    def _require_batch_v1(self) -> Any:
        if self._batch_v1 is None:
            raise RuntimeError(
                "KubernetesJobsExecutor: BatchV1Api not initialised — call open(plan) first"
            )
        return self._batch_v1

    def _require_core_v1(self) -> Any:
        if self._core_v1 is None:
            raise RuntimeError(
                "KubernetesJobsExecutor: CoreV1Api not initialised — call open(plan) first"
            )
        return self._core_v1

    def _outcome_from_dict(self, raw: dict[str, Any]) -> CellOutcome:
        from eval_harness.runner.run_eval import CellOutcome

        cell_id = raw["cell_id"]
        case, variant = self._cell_index[cell_id]
        trace = Trace.model_validate(raw["trace"])
        results = [
            EvaluationResult.model_validate(r) for r in raw.get("results", [])
        ]
        return CellOutcome(case=case, variant=variant, trace=trace, results=results)

    def _build_failure_outcome(self, cell_id: str) -> CellOutcome:
        from eval_harness.runner.run_eval import CellOutcome

        case, variant = self._cell_index[cell_id]
        trace = Trace.from_error(
            case.id,
            variant.name,
            "adapter_error",
            "kubernetes job reached Failed state without writing an outcome",
        )
        return CellOutcome(case=case, variant=variant, trace=trace, results=[])

    def _build_missing_outcome_failure(self, cell_id: str) -> CellOutcome:
        from eval_harness.runner.run_eval import CellOutcome

        case, variant = self._cell_index[cell_id]
        trace = Trace.from_error(
            case.id,
            variant.name,
            "adapter_error",
            "kubernetes job completed but no outcome was found at the configured ObjectStorage key",
        )
        return CellOutcome(case=case, variant=variant, trace=trace, results=[])

    def _build_timeout_outcome(self, cell_id: str) -> CellOutcome:
        from eval_harness.runner.run_eval import CellOutcome

        case, variant = self._cell_index[cell_id]
        trace = Trace.from_error(
            case.id,
            variant.name,
            "timeout",
            f"kubernetes job exceeded the executor timeout of {self._timeout_seconds}s",
        )
        return CellOutcome(case=case, variant=variant, trace=trace, results=[])

    def _job_name_for(self, cell: CellDescriptor) -> str:
        return f"evalh-{_k8s_name_suffix(cell.cell_id)}"

    def _configmap_name_for(self, cell: CellDescriptor) -> str:
        return f"evalh-cm-{_k8s_name_suffix(cell.cell_id)}"

    def _outcome_key_for(self, cell: CellDescriptor) -> str:
        return f"cells/{cell.cell_id}/outcome.json"


def _classify_job_status(status: Any) -> str | None:
    """Return ``"complete"`` / ``"failed"`` for a terminal Job, ``None``
    while still running. Accepts either a real ``V1Job`` (object with
    ``.status``) or a dict — kubernetes' models support both shapes when
    callers pass dicts to ``read_namespaced_job_status``."""
    raw = _get(status, "status")
    if raw is None:
        return None
    succeeded = _get(raw, "succeeded")
    failed = _get(raw, "failed")
    if succeeded and int(succeeded) > 0:
        return "complete"
    if failed and int(failed) > 0:
        return "failed"
    conditions = _get(raw, "conditions") or []
    for cond in conditions:
        ctype = _get(cond, "type")
        cstatus = _get(cond, "status")
        if cstatus != "True":
            continue
        if ctype == "Complete":
            return "complete"
        if ctype in ("Failed", "FailureTarget"):
            return "failed"
    return None


def _get(obj: Any, key: str) -> Any:
    """Read ``key`` from either a dict or an object with that attribute.
    kubernetes client models expose snake_case attributes; dicts use the
    same names; this helper papers over the difference."""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _k8s_name_suffix(cell_id: str) -> str:
    """Derive a DNS-1123-compliant suffix from a cell id.

    Cell ids look like ``r1::c2::stub::0123456789ab`` — keep the hash
    plus a short prefix for human readability. Kubernetes names must be
    lowercase alphanumeric + hyphens and ≤ 63 chars total. Anything
    outside that gets hashed so two cell ids that differ only in
    punctuation never collide."""
    lowered = cell_id.lower()
    safe = _K8S_NAME_RE.sub("-", lowered).strip("-")
    if len(safe) <= 50:
        return safe or _hash_suffix(cell_id)
    # Long ids: keep the tail (hash slot) and append a fingerprint so
    # two long-but-similar ids stay distinct.
    return f"{safe[-40:]}-{_hash_suffix(cell_id)}"


def _hash_suffix(cell_id: str) -> str:
    return hashlib.sha256(cell_id.encode("utf-8")).hexdigest()[:8]


__all__ = ["KubernetesJobsExecutor"]
