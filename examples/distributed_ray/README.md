# Example: distributed_ray

> **Requires `ray[default]` installed locally (or a reachable Ray cluster). Excluded from CI smoke runs.**

The Ray-executor demo. The same eval as [`examples/regression_gate/`](../regression_gate/) — same agent, same cases, same evaluators, same pass criteria — but dispatched through `run.executor.type: ray` instead of the default in-process executor. Each `(case, variant)` cell becomes a `ray.remote` task; the worker rebuilds adapters from `eval_config_dict` via the entry-point layer. **Config travels, code doesn't.**

This is the canonical shape for *"my eval suite is large — can I run it on a Ray cluster instead of one machine?"*

## Files

| File | What it is |
|---|---|
| [`eval.yaml`](eval.yaml) | A copy of [`regression_gate/eval.yaml`](../regression_gate/eval.yaml) with one delta: a `run.executor.type: ray` block plus `pools: {llm: 8}`. Dataset, agent, evaluators, and pass criteria are reused unchanged. |

The agent, the cases, and the evaluators all live elsewhere — the dataset is linked from [`../tiny_demo/cases.yaml`](../tiny_demo/cases.yaml), and `systems[0].target` points at [`../regression_gate/agent.py`](../regression_gate/agent.py). There is nothing example-specific to read except the YAML.

## Required environment

```bash
pip install 'eval-harness[ray]'    # installs the harness's ray executor dependency
pip install 'ray[default]'         # gives the dashboard + cluster utilities the spec calls out
```

The Ray executor's import guard inside `eval_harness.core.executors.ray_executor.RayExecutor.__init__` raises `ConfigError` with the install hint if the `ray` package is missing (mirrors `sqlite_store.py`), so a missing extra fails at plan time — before any cell dispatches.

> **Online-only? No network. Distributed-only.** This example doesn't need an API key, and the stub agent answers offline. It does need a Python process where `import ray` succeeds. Local single-machine runs work; the Ray executor's `address: "auto"` default boots an in-process cluster.

CI does not exercise this example. The repo's smoke workflow only runs `tiny_demo`, and the executor's own integration tests carry `@pytest.mark.ray` — excluded from the default pytest invocation (same posture as `modal`, `celery`, and `kubernetes`).

## Run it

```bash
evalh run examples/distributed_ray/eval.yaml
```

Expected: the same three cases pass that the regression_gate run passes, single variant `agent_stub`, the only visible difference is `runs/<id>/summary.yaml`'s executor block reading `type: ray` instead of `type: local`.

## What happens, in order

1. The runner expands `cases × variants` — 3 × 1 = 3 cells — and the orchestrator builds one `CellDescriptor` per cell. The descriptor carries `eval_config_dict` (the full parsed config) and `case_dict` (a re-validatable case payload), but **not** any live adapter or evaluator instance.
2. The executor registry resolves `run.executor.type: ray` to `RayExecutor` via the `eval_harness.executors` entry point. The executor's constructor imports `ray`; missing package → `ConfigError` with install hint.
3. The executor calls `ray.init(address="auto")` — connects to a running cluster if `RAY_ADDRESS` is set, otherwise starts a local one. The `object_store_memory` default (~75 MiB) keeps the boot fitting inside small `/dev/shm` budgets.
4. Each `CellDescriptor` is submitted as a `ray.remote` task. The worker calls `worker_run_cell_sync(...)`, which **rebuilds the system adapter, the workspace adapter, and the evaluators from `eval_config_dict`** using the same factory + entry-point layer the orchestrator uses. The worker never deserialises a pickled adapter.
5. Each worker returns an outcome dict — trace, results, latency, error. The orchestrator streams outcomes into the `SummaryAggregator`, the cost accumulator, and the configured trace stores.
6. `runs/<id>/summary.yaml` lands locally as usual; the only visible change against the regression_gate run is the executor metadata block.

## Why this works

Two design choices make this trivial.

**Config travels, code doesn't.** The `CellDescriptor` is JSON-shaped — `eval_config_dict`, `case_dict`, identifiers, hashes. Workers reconstitute code through the same entry-point-driven factories the orchestrator uses, so a custom evaluator that registers under `eval_harness.evaluators` works identically in-process and on a Ray worker — provided the worker has the same Python package surface installed. This is why the executor's `runtime_env.pip` knob exists: you can pin `["eval-harness", "my-plugin"]` and Ray installs the matching set on every worker.

**The runner doesn't know which executor it's talking to.** `eval_harness.runner.run_eval` only ever calls the `Executor` Protocol — `open()`, `submit_cell()`, `await_outcome()`, `close()`. There is no `if executor_type == "ray":` branch in the runner. Swapping `local` for `ray` is one YAML line, not a code change. The same machinery is what makes `modal`, `celery`, and `kubernetes` executors equally one-line swaps.

The `pools: {llm: 8}` block declares a named capacity pool. Any variant that adds `systems[<n>].pool: llm` will route its cells through that semaphore — useful when several variants share a rate-limited LLM backend and you want a single ceiling across them. The shipped variant here doesn't claim the pool (the stub agent doesn't talk to an LLM), so the declaration is decorative for this example and live the moment you point a variant at a real model.

For deeper context — cells vs functions, deterministic cell IDs, idempotent trace stores, the full executor protocol — see [`docs/Executors.md`](../../docs/Executors.md).

## Extending it

- **Connect to a real cluster.** Add `address: ray://head-node:10001` (or set `RAY_ADDRESS`) and `runtime_env: {pip: ["eval-harness", "my-plugin"]}`. The example's three cells run on the cluster; the orchestrator stays local.
- **Demonstrate the pool.** Add a variant that uses an LLM and tag it `pool: llm`. Run the suite with cases × 2 variants; the executor enforces the 8-cell ceiling on the llm-pool variant independently of the per-variant semaphore.
- **Swap to a different executor.** Change `type: ray` to `type: modal`, `type: celery`, or `type: kubernetes`. The rest of the file is unchanged; each executor's required extra is documented in [`docs/Executors.md`](../../docs/Executors.md).
- **Bench at scale.** `benchmarks/distributed_1m.py` is the manual benchmark that exercises this executor on cluster-scale workloads. The example here is the smallest legible config for the same plumbing.
