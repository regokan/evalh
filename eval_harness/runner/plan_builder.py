from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from eval_harness.core.config import EvalConfig, RetryPolicy, SystemConfig
from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, RunVariant
from eval_harness.core.time import make_run_id
from eval_harness.factories.dataset_adapter_factory import DatasetAdapterFactory
from eval_harness.factories.evaluator_factory import EvaluatorFactory
from eval_harness.factories.system_adapter_factory import SystemAdapterFactory
from eval_harness.factories.trace_store_factory import TraceStoreFactory
from eval_harness.factories.workspace_factory import WorkspaceFactory

_STRUCTURAL_KEYS = frozenset({"name", "adapter", "metadata"})

if TYPE_CHECKING:
    from eval_harness.adapters.system.base import SystemAdapter
    from eval_harness.adapters.trace.base import TraceStore
    from eval_harness.adapters.workspace.base import WorkspaceAdapter
    from eval_harness.evaluators.base import Evaluator


@dataclass
class RunPlan:
    config: EvalConfig
    run_id: str
    run_dir: Path
    cases: list[EvalCase]
    variants: list[RunVariant]
    system_adapters: dict[str, SystemAdapter]
    trace_store: TraceStore
    workspace: WorkspaceAdapter | None
    evaluators: list[Evaluator]
    retry_policy: RetryPolicy
    baseline_variant: str | None


@dataclass
class Factories:
    dataset: DatasetAdapterFactory
    system: SystemAdapterFactory
    evaluator: EvaluatorFactory
    workspace: WorkspaceFactory
    trace_store: TraceStoreFactory


def _default_factories() -> Factories:
    f = Factories(
        dataset=DatasetAdapterFactory(),
        system=SystemAdapterFactory(),
        evaluator=EvaluatorFactory(),
        workspace=WorkspaceFactory(),
        trace_store=TraceStoreFactory(),
    )
    f.dataset.load_entry_points()
    f.system.load_entry_points()
    f.evaluator.load_entry_points()
    f.workspace.load_entry_points()
    f.trace_store.load_entry_points()
    return f


async def build_plan(
    config: EvalConfig,
    config_path: Path,
    factories: Factories | None = None,
) -> RunPlan:
    facs = factories or _default_factories()

    run_id = make_run_id(config.eval.name)
    if not config.output:
        raise ConfigError("config has no output[] stores")
    output_cfg = config.output[0]
    if not output_cfg.path:
        raise ConfigError(f"output[0] '{output_cfg.type}' is missing 'path'")
    run_dir = Path(output_cfg.path) / run_id

    dataset_adapter = facs.dataset.build(config.dataset.model_dump())
    cases = await dataset_adapter.load_cases()
    cases = _apply_filter(cases, config.dataset.filter)
    cases = _apply_sample(cases, config.dataset.sample, run_id)

    variants: list[RunVariant] = []
    system_adapters: dict[str, SystemAdapter] = {}
    for sys_cfg in config.systems:
        variant_config = _system_extras(sys_cfg)
        variant = RunVariant(
            name=sys_cfg.name,
            adapter=sys_cfg.adapter,
            config=variant_config,
            metadata=dict(sys_cfg.metadata),
        )
        variants.append(variant)
        system_adapters[variant.name] = facs.system.build(variant)

    trace_store = facs.trace_store.build(output_cfg.model_dump())
    if hasattr(trace_store, "rendered_config"):
        trace_store.rendered_config = config.model_dump(mode="python")

    workspace: WorkspaceAdapter | None = None
    if config.workspace is not None:
        workspace = facs.workspace.build(config.workspace.model_dump())

    evaluators = [facs.evaluator.build(e) for e in config.evaluators]

    baseline = config.run.baseline_variant
    known_variants = set(system_adapters)
    if baseline is not None and baseline not in known_variants:
        raise ConfigError(
            f"run.baseline_variant '{baseline}' not found in systems[]; "
            f"defined: {sorted(known_variants)}"
        )

    return RunPlan(
        config=config,
        run_id=run_id,
        run_dir=run_dir,
        cases=cases,
        variants=variants,
        system_adapters=system_adapters,
        trace_store=trace_store,
        workspace=workspace,
        evaluators=evaluators,
        retry_policy=config.run.retry,
        baseline_variant=baseline,
    )


def _system_extras(sys_cfg: SystemConfig) -> dict[str, object]:
    dumped = sys_cfg.model_dump()
    return {k: v for k, v in dumped.items() if k not in _STRUCTURAL_KEYS}


def _apply_filter(cases: list[EvalCase], spec: dict[str, object]) -> list[EvalCase]:
    if not spec:
        return cases
    out: list[EvalCase] = []
    for case in cases:
        dumped = case.model_dump()
        if all(_path_matches(dumped, k, v) for k, v in spec.items()):
            out.append(case)
    return out


def _path_matches(data: object, dotted: str, expected: object) -> bool:
    cur: object = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    if isinstance(expected, list):
        return cur in expected
    return cur == expected


def _apply_sample(cases: list[EvalCase], sample: int | None, run_id: str) -> list[EvalCase]:
    if sample is None or sample >= len(cases):
        return cases
    rng = random.Random(run_id)
    return rng.sample(cases, sample)
