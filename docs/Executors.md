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

## Subsequent v2 beads

- **Local executor + capacity pools + 10K perf gate** — the in-process
  executor; registers as `local`.
- **ObjectStorage Protocol + local + cloud via fsspec** — runs sharing
  artifacts via S3 / GCS instead of localfs only.
- **Modal / Kubernetes Jobs / Celery / Ray executors** — each its own
  bead, each registers via the entry-point group.
- **Distributed-1M benchmark** — proves the per-cell envelope at scale.
