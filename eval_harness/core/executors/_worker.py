"""Per-cell worker — shared by distributed executors (Modal / Ray / K8s).

A worker rehydrates adapters + evaluators from a serialized
``CellDescriptor`` via the existing factory + entry-point layer. **Config
travels; code doesn't.** Workers in containers carry the entry-point
sets they installed; the runner trusts them to resolve names the same way
the orchestrator did.

The worker returns a plain dict so distributed executors can hand the
result back to the orchestrator without depending on this module's
concrete types. The orchestrator's `Executor.await_outcome` is
responsible for shaping the dict back into a `CellOutcome` if needed.
"""

from __future__ import annotations

import asyncio
from typing import Any

from eval_harness.core.config import EvalConfig
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    RunVariant,
    Trace,
    TraceError,
)
from eval_harness.core.time import utc_now
from eval_harness.factories.evaluator_factory import EvaluatorFactory
from eval_harness.factories.system_adapter_factory import SystemAdapterFactory


async def worker_run_cell(
    cell_dict: dict[str, Any], *, timeout_seconds: float | None = None
) -> dict[str, Any]:
    """Execute one cell in this process. Returns ``{"trace": ..., "results": ...}``.

    Inputs come from ``CellDescriptor.model_dump()``; outputs are
    JSON-roundtrippable dicts so transport layers (Modal, Ray) don't have
    to know about our Pydantic models. The caller (orchestrator-side
    executor) re-validates back into models when it wants typed access.

    NOT a full ``_run_one`` reimplementation: the worker handles a single
    cell with adapter + evaluators; workspace + trace-store + sink-mirror
    persistence stay on the orchestrator. That keeps the worker pickle-
    free and side-effect-light, which is the v2 contract.
    """
    case = EvalCase.model_validate(cell_dict["case_dict"])
    eval_config = EvalConfig.model_validate(cell_dict["eval_config_dict"])
    variant_name = cell_dict["variant_name"]
    run_id = cell_dict.get("run_id", "")

    # Locate the right systems[] entry; everything else is metadata
    # the worker doesn't need to touch.
    sys_cfg = next((s for s in eval_config.systems if s.name == variant_name), None)
    if sys_cfg is None:
        raise RuntimeError(
            f"worker: variant {variant_name!r} not found in eval_config.systems "
            f"(known: {[s.name for s in eval_config.systems]})"
        )
    variant_config = _system_extras(sys_cfg)
    variant = RunVariant(
        name=sys_cfg.name,
        adapter=sys_cfg.adapter,
        config=variant_config,
        metadata=dict(sys_cfg.metadata),
    )

    system_factory = SystemAdapterFactory()
    system_factory.load_entry_points()
    adapter = system_factory.build(variant)

    evaluator_factory = EvaluatorFactory()
    evaluator_factory.load_entry_points()
    evaluators = [evaluator_factory.build(e) for e in eval_config.evaluators]

    started_at = utc_now()
    try:
        async with adapter:
            try:
                if timeout_seconds is not None:
                    trace = await asyncio.wait_for(
                        adapter.run(case, variant, None),
                        timeout=timeout_seconds,
                    )
                else:
                    trace = await adapter.run(case, variant, None)
            except TimeoutError as e:
                trace = Trace.from_error(case.id, variant.name, "timeout", str(e) or "timed out")
            except Exception as e:
                trace = Trace.from_error(
                    case.id,
                    variant.name,
                    "adapter_error",
                    f"{type(e).__name__}: {e}",
                )
    except Exception as e:  # pragma: no cover — defensive
        trace = Trace.from_error(
            case.id, variant.name, "adapter_error", f"{type(e).__name__}: {e}"
        )
    finished_at = utc_now()

    _enforce_invariants(trace, run_id, case, variant, started_at, finished_at)

    # Evaluators run in-process; failures become EvaluationResult.error
    # rows so the cell still completes.
    raw_results = await asyncio.gather(
        *(ev.evaluate(case, trace, None) for ev in evaluators),
        return_exceptions=True,
    )
    normalized: list[EvaluationResult] = []
    for raw, ev in zip(raw_results, evaluators, strict=True):
        if isinstance(raw, EvaluationResult):
            raw.run_id = run_id
            raw.case_id = case.id
            raw.variant_name = variant.name
            normalized.append(raw)
        elif isinstance(raw, BaseException):
            now = utc_now()
            normalized.append(
                EvaluationResult(
                    run_id=run_id,
                    case_id=case.id,
                    variant_name=variant.name,
                    evaluator=ev.name,
                    evaluator_type=ev.type,
                    passed=False,
                    reason=f"evaluator '{ev.name}' crashed: {raw}",
                    started_at=now,
                    finished_at=now,
                    latency_ms=0,
                    error=TraceError(type=type(raw).__name__, message=str(raw)),
                )
            )

    return {
        "cell_id": cell_dict["cell_id"],
        "trace": trace.model_dump(mode="json"),
        "results": [r.model_dump(mode="json") for r in normalized],
    }


_STRUCTURAL_KEYS = frozenset({"name", "adapter", "metadata", "enrich_trace_from", "pool"})


def _system_extras(sys_cfg: Any) -> dict[str, Any]:
    """Mirror `plan_builder._system_extras`. Strips structural keys so the
    SystemAdapter sees only its own config."""
    dumped = sys_cfg.model_dump()
    return {k: v for k, v in dumped.items() if k not in _STRUCTURAL_KEYS}


def _enforce_invariants(
    trace: Trace,
    run_id: str,
    case: EvalCase,
    variant: RunVariant,
    started_at: Any,
    finished_at: Any,
) -> None:
    """Mirror the runner's invariants helper: join keys + wall-time latency,
    skipping the wall-time overwrite for replay traces (ev-s95). Importing
    the runner module from a worker pulls in trace-store / aggregator
    machinery the worker doesn't need; re-stating the contract is cheaper."""
    trace.run_id = run_id
    trace.case_id = case.id
    trace.variant_name = variant.name
    if trace.extra.get("source") == "replay":
        return
    trace.started_at = started_at
    trace.finished_at = finished_at
    delta_seconds = (finished_at - started_at).total_seconds()
    trace.latency_ms = max(0, int(delta_seconds * 1000))


def worker_run_cell_sync(
    cell_dict: dict[str, Any], *, timeout_seconds: float | None = None
) -> dict[str, Any]:
    """Synchronous entry point for transports (Modal default, Ray Actor
    methods) that prefer ``def`` over ``async def``. Drives an asyncio
    event loop locally — one per worker invocation, isolated from the
    orchestrator's loop."""
    return asyncio.run(worker_run_cell(cell_dict, timeout_seconds=timeout_seconds))


__all__ = ["worker_run_cell", "worker_run_cell_sync"]
