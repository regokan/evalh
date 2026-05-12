from __future__ import annotations

import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from eval_harness.core.config import EvalConfig, RetryPolicy, SystemConfig
from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, RunVariant
from eval_harness.core.price_tables import PriceTable, load_price_table
from eval_harness.core.time import make_run_id
from eval_harness.factories.dataset_adapter_factory import DatasetAdapterFactory
from eval_harness.factories.evaluator_factory import EvaluatorFactory
from eval_harness.factories.system_adapter_factory import SystemAdapterFactory
from eval_harness.factories.trace_enricher_factory import TraceEnricherFactory
from eval_harness.factories.trace_store_factory import TraceStoreFactory
from eval_harness.factories.workspace_factory import WorkspaceFactory

_STRUCTURAL_KEYS = frozenset({"name", "adapter", "metadata", "enrich_trace_from"})

if TYPE_CHECKING:
    from eval_harness.adapters.enricher.base import TraceEnricher
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
    # First (canonical) TraceStore — failures here abort the run. v0 single-
    # sink behaviour is preserved by always exposing the first store here.
    trace_store: TraceStore
    workspace: WorkspaceAdapter | None
    evaluators: list[Evaluator]
    retry_policy: RetryPolicy
    baseline_variant: str | None
    price_table: PriceTable | None = None
    # Per-variant TraceEnricher chains. Empty list = no enrichment. Order
    # matters: the runner runs them in this order between SystemAdapter and
    # evaluators.
    enrichers: dict[str, list[TraceEnricher]] = field(default_factory=dict)
    # Best-effort mirror sinks declared after the canonical one in
    # `eval.yaml > output:[1..]`. Failures land in
    # `RunSummary.sink_errors`; runs do NOT abort.
    secondary_trace_stores: list[TraceStore] = field(default_factory=list)
    # Optional whitelist of `(case_id, variant_name)` cells. When set, run_eval
    # executes only these specific cells (instead of the full cases x variants
    # product). Used by `evalh run --retry-only-failed` to amend an existing
    # run.
    cell_filter: frozenset[tuple[str, str]] | None = None


@dataclass
class Factories:
    dataset: DatasetAdapterFactory
    system: SystemAdapterFactory
    evaluator: EvaluatorFactory
    workspace: WorkspaceFactory
    trace_store: TraceStoreFactory
    trace_enricher: TraceEnricherFactory


def _default_factories() -> Factories:
    f = Factories(
        dataset=DatasetAdapterFactory(),
        system=SystemAdapterFactory(),
        evaluator=EvaluatorFactory(),
        workspace=WorkspaceFactory(),
        trace_store=TraceStoreFactory(),
        trace_enricher=TraceEnricherFactory(),
    )
    f.dataset.load_entry_points()
    f.system.load_entry_points()
    f.evaluator.load_entry_points()
    f.workspace.load_entry_points()
    f.trace_store.load_entry_points()
    f.trace_enricher.load_entry_points()
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
    enrichers: dict[str, list[TraceEnricher]] = {}
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
        enrichers[variant.name] = [
            facs.trace_enricher.build(spec) for spec in sys_cfg.enrich_trace_from
        ]

    # Build every declared sink. First is canonical; the rest are best-effort
    # mirrors. The canonical sink owns the on-disk run_dir; we don't try to
    # generalise that to secondary sinks because most non-local backends
    # (sqlite, postgres, langfuse, …) don't need a host path.
    rendered_config = config.model_dump(mode="python")
    all_stores: list[TraceStore] = []
    for out_cfg in config.output:
        store = facs.trace_store.build(out_cfg.model_dump())
        if hasattr(store, "rendered_config"):
            store.rendered_config = rendered_config
        all_stores.append(store)
    trace_store = all_stores[0]
    secondary_trace_stores = all_stores[1:]

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

    price_table = _build_price_table(config, config_path)

    return RunPlan(
        config=config,
        run_id=run_id,
        run_dir=run_dir,
        secondary_trace_stores=secondary_trace_stores,
        cases=cases,
        variants=variants,
        system_adapters=system_adapters,
        trace_store=trace_store,
        workspace=workspace,
        evaluators=evaluators,
        retry_policy=config.run.retry,
        baseline_variant=baseline,
        price_table=price_table,
        enrichers=enrichers,
    )


def _build_price_table(config: EvalConfig, config_path: Path) -> PriceTable:
    raw = config.metrics.price_table_path
    if raw is None:
        return load_price_table(None)
    path = Path(raw)
    if not path.is_absolute():
        path = (config_path.parent / path).resolve()
    return load_price_table(path)


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
