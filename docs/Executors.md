# Executors — the v2 dispatch primitive

> The unit of distribution is a cell. The runner becomes a coordinator.

## What an Executor does

The runner builds one `CellDescriptor` per (case, variant) and submits it
to an `Executor`. The executor carries the work in its environment —
local asyncio tasks, Modal containers, Kubernetes Jobs, Celery workers,
Ray actors — and hands back an opaque `CellHandle`. The runner awaits
handles, streams `CellOutcome`s into the summary aggregator, and writes
results through the configured `TraceStore`s.

```python
class Executor(Protocol):
    async def open(self, plan: RunPlan) -> None: ...
    async def submit_cell(self, cell: CellDescriptor) -> CellHandle: ...
    async def await_outcome(self, handle: CellHandle) -> CellOutcome: ...
    async def await_all(self, handles: list[CellHandle]) -> list[CellOutcome]: ...
    async def close(self) -> None: ...
```

Built-in executors register under the `eval_harness.executors`
entry-point group. The Local executor (F2) registers `local`;
distributed executors register from their own packages.

---

## Cells, not functions

**Workers rebuild adapters from `eval_config_dict` via the existing
factory + entry-point layer. They do NOT receive serialized adapter
instances.**

The pickle-the-function path looks attractive for trivial cases — and
breaks the moment custom-evaluator entry-points (the v0.1 plugin path)
enter the picture. A worker on another machine can't unpickle
`my_company.evals.MyJudge` unless that package + entry-point set is
installed in the worker's environment, and even then pickle's
unmarshalling of class references is a footgun for users who don't
control both sides of the wire.

Cells carry **config**. Workers reconstitute **code** through the same
factories the runner uses.

```python
class CellDescriptor(BaseModel):
    cell_id: str             # deterministic; see compute_cell_id
    run_id: str
    case_id: str
    variant_name: str
    config_hash: str
    eval_config_dict: dict   # workers rebuild adapters from this
    case_dict: dict          # workers re-validate EvalCase via pydantic
    workspace_kind: str | None
    pool: str | None         # capacity-pool routing (F2)
```

---

## Deterministic cell IDs + idempotency

`compute_cell_id(run_id, case_id, variant_name, config_slice)` hashes
the config slice that affects this cell (variant block + the evaluator
blocks that touch it) and returns a stable id:

```
<run_id>::<case_id>::<variant_name>::<sha256-12char>
```

Same inputs across machines → same id. The trace-store idempotency
contract leans on that: `TraceStore.save_trace_idempotent(trace,
cell_id)` is a no-op on a successful replay and a retry-overwrite on
an error replay. Three canonical sinks implement it for real:

| Sink | Storage shape |
|---|---|
| `local_files` | sidecar marker at `runs/<id>/cells/<cell_id>.success.marker` |
| `sqlite`      | `traces.cell_id TEXT` column + `error_type` guard on UPSERT |
| `postgres`    | `eval_traces.cell_id TEXT` + indexed; `ON CONFLICT … WHERE existing.error_type IS NOT NULL` |

Other stores (otel, langfuse, phoenix, arize, braintrust, webhook)
inherit the always-write fallback. The idempotency contract lives at
the canonical sink — see [Adapters.md → Trace store idempotency](Adapters.md#trace-store-idempotency).

---

## Lifecycle

```text
runner.run_eval(plan):
    executor = executor_registry.resolve(plan.executor_name)
    await executor.open(plan)
    try:
        handles = [await executor.submit_cell(cell) for cell in plan.cells()]
        outcomes = await executor.await_all(handles)
    finally:
        await executor.close()
```

`open` happens once per run; `submit_cell` may be called concurrently.
Ordering is the runner's responsibility — variant semaphores live in
the executor (Local) or are emergent from cluster capacity (Modal /
K8s).

---

## Kubernetes Jobs executor

`KubernetesJobsExecutor` registers as `kubernetes` under the
`eval_harness.executors` entry-point group when the `[kubernetes]` extra
is installed. Each cell becomes a `batch/v1` Job whose pod runs the
`evalh-cell-worker` console-script. Highest startup cost of the built-in
executors (image pull + pod spin-up dominate fast cells) but the most
production-realistic shape for shops that already run K8s.

### Deployment recipe

1. Build a container image carrying `eval-harness` plus your plugin
   packages and any extras the run needs (`anthropic`, `openai`, your
   custom adapters, …). The orchestrator and pod must resolve the same
   entry-point set — that's the contract.

   ```dockerfile
   FROM python:3.12-slim
   RUN pip install --no-cache-dir 'eval-harness[anthropic]' my-plugin-package
   ENTRYPOINT ["evalh-cell-worker"]
   ```

2. Decide where the pod writes its outcome JSON. Anything fsspec can
   reach works — typically `s3://...`, `gs://...`, or a shared
   `file://` mount when the cluster has one. The orchestrator reads the
   same URL.

3. Configure the run:

   ```yaml
   run:
     executor:
       type: kubernetes
       config:
         image: ghcr.io/your-org/evalh-worker:2026.05
         namespace: evals
         service_account: evalh-worker
         resources:
           requests: { cpu: "500m", memory: "1Gi" }
           limits:   { cpu: "2",    memory: "4Gi" }
         object_storage:
           url: s3://your-bucket/evalh-runs
         poll_interval_seconds: 2
         timeout_seconds: 1800
   ```

   The pod runs `evalh-cell-worker` as its container command (set by the
   executor — your image's `ENTRYPOINT` can be the same or omitted).

4. RBAC: the orchestrator needs `create / get / delete` on
   `batch/v1/jobs` and `core/v1/configmaps` in `namespace`. The pod's
   service account needs read access to whatever Secret carries the
   ObjectStorage credentials.

### Payload routing

Cell payloads ≤ 32 KiB ride a single `EVALH_CELL_PAYLOAD` env var. Above
that, the executor creates a backing `ConfigMap` and mounts it at
`/etc/evalh/cell.json`; the pod reads `EVALH_CELL_PAYLOAD_PATH` to find
it. Pod env limits are aggregate ~1 MB, so 32 KiB leaves headroom for
the storage URL + credentials + timeout env vars and avoids admission
controllers that cap individual values lower.

### Result piping

`await_outcome` polls `read_namespaced_job_status` every
`poll_interval_seconds` until the Job reaches `Complete` / `Failed`,
then reads the outcome JSON from `cells/<cell_id>/outcome.json` under
the configured `object_storage.url`. Job + ConfigMap are deleted after
the outcome is fetched (best-effort — `ttlSecondsAfterFinished` is the
backstop for orphans).

### Trade-offs

The Job-per-cell model is wasteful for fast cells (pod startup
dominates). It's the right shape for slow cells — LLM evals where each
call is multiple seconds — and the right shape for clusters where each
pod gets its own image / IAM / secrets without your orchestrator caring.
Shops that want denser packing can write a custom Executor that batches
cells per pod; that's their judgement call, not the default.

---

## Subsequent v2 beads

- **Local executor + capacity pools + 10K perf gate** — the in-process
  executor; registers as `local`.
- **ObjectStorage Protocol + local + cloud via fsspec** — runs sharing
  artifacts via S3 / GCS instead of localfs only.
- **Modal / Kubernetes Jobs / Celery / Ray executors** — each its own
  bead, each registers via the entry-point group.
- **Distributed-1M benchmark** — proves the per-cell envelope at scale.

---

## CeleryExecutor

Each cell becomes a ``celery.Celery.send_task`` call against a broker
(Redis or AMQP) with a matching result backend. Workers boot separately
against the same broker via ``celery -A <module> worker`` and resolve
the registered ``evalh.run_cell`` task by name; the worker body is the
shared ``_worker.worker_run_cell_sync`` Modal and Ray also use, so
workers behave identically across executors.

```yaml
run:
  executor:
    type: celery
    broker_url: redis://localhost:6379/0       # required
    result_backend: redis://localhost:6379/0   # defaults to broker_url
    task_name: evalh.run_cell                  # default
    timeout: 600                               # AsyncResult.get timeout (s)
```

This is the heaviest infra footprint of the four distributed executors:
the broker, the result backend, and a worker pool all have to be
running independently of the orchestrator. The integration test
(``@pytest.mark.celery``) honours ``EVALH_TEST_REDIS_URL`` — set it to
a reachable Redis URL to run the live-broker tests; CI excludes the
marker by default because CI workflows don't ship a Redis service.

---

## RayExecutor

Each cell becomes a ``ray.remote`` task. Ray's local-cluster mode is
sufficient for the integration tests when the host has a reasonable
``/dev/shm``; the ``RayExecutor`` config knob ``object_store_memory``
defaults to ~75 MiB so a single-machine boot fits inside small shared-
memory budgets. Real-cluster runs are driven by the manual benchmark
(``benchmarks/distributed_1m.py``).

```yaml
run:
  executor:
    type: ray
    address: auto                              # connect to running cluster
    runtime_env:
      pip: ["eval-harness", "my-plugin"]       # entry-point set must match
    num_cpus_per_cell: 1
    num_gpus_per_cell: null
    object_store_memory: null                  # auto-size on real clusters
```

**CI posture:** ``@pytest.mark.ray`` tests require a real Ray cluster
or a Linux dev environment with sufficient ``/dev/shm``. CI does NOT
run ``@pytest.mark.ray`` (same posture as ``@pytest.mark.modal`` and
``@pytest.mark.kubernetes``): GitHub Actions runners can't fork Ray
workers reliably even with a reduced plasma store, so the marker is
excluded from the workflow and verified locally instead.
