from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from eval_harness.core.config import PassCriteria
from eval_harness.core.models import (
    ComparisonReport,
    EvaluationResult,
    EvaluatorRollup,
    EvaluatorVariantRollup,
    RunSummary,
    VariantSummary,
)
from eval_harness.core.time import utc_now
from eval_harness.reports.comparison_writer import build_comparison

if TYPE_CHECKING:
    from eval_harness.runner.plan_builder import RunPlan
    from eval_harness.runner.run_eval import CellOutcome


def build_summary(outcomes: Iterable[CellOutcome], plan: RunPlan) -> RunSummary:
    outcomes_list = list(outcomes)

    by_variant: dict[str, list[CellOutcome]] = {v.name: [] for v in plan.variants}
    for outcome in outcomes_list:
        by_variant[outcome.variant.name].append(outcome)

    case_pass_by_variant: dict[str, dict[str, bool]] = {}
    variant_summaries: list[VariantSummary] = []
    for variant in plan.variants:
        cells = by_variant[variant.name]
        case_pass: dict[str, bool] = {}
        for cell in cells:
            case_pass[cell.case.id] = _case_passes(cell.results, plan.config.pass_criteria)
        case_pass_by_variant[variant.name] = case_pass

        cases_total = len(cells)
        cases_passed = sum(1 for ok in case_pass.values() if ok)
        cases_errored = sum(1 for cell in cells if cell.trace.error is not None)

        latencies = [cell.trace.latency_ms for cell in cells]
        costs = [
            cell.trace.metrics.cost_usd
            for cell in cells
            if cell.trace.metrics.cost_usd is not None
        ]
        tokens_in = [
            cell.trace.metrics.token_input
            for cell in cells
            if cell.trace.metrics.token_input is not None
        ]
        tokens_out = [
            cell.trace.metrics.token_output
            for cell in cells
            if cell.trace.metrics.token_output is not None
        ]

        variant_summaries.append(
            VariantSummary(
                name=variant.name,
                cases_total=cases_total,
                cases_passed=cases_passed,
                cases_errored=cases_errored,
                pass_rate=_safe_ratio(cases_passed, cases_total),
                avg_latency_ms=_avg(latencies),
                avg_cost_usd=_avg(costs) if costs else None,
                avg_tokens_input=_avg(tokens_in) if tokens_in else None,
                avg_tokens_output=_avg(tokens_out) if tokens_out else None,
            )
        )

    by_evaluator = _evaluator_rollups(outcomes_list, [v.name for v in plan.variants])

    comparison: ComparisonReport | None = None
    if plan.baseline_variant is not None:
        comparison = build_comparison(
            baseline=plan.baseline_variant,
            variants=plan.variants,
            variant_summaries=variant_summaries,
            case_pass_by_variant=case_pass_by_variant,
        )

    now = utc_now()
    return RunSummary(
        run_id=plan.run_id,
        started_at=now,
        finished_at=now,
        config_path=str(plan.run_dir),
        config_hash="",
        cases_total=len(plan.cases),
        variants=variant_summaries,
        by_evaluator=by_evaluator,
        comparison=comparison,
    )


def _case_passes(results: list[EvaluationResult], criteria: PassCriteria) -> bool:
    if not results:
        return False
    pass_by_name = {r.evaluator: r.passed and r.error is None for r in results}

    if not criteria.all_required and not criteria.any_required:
        return all(pass_by_name.values())

    all_ok = all(pass_by_name.get(name, False) for name in criteria.all_required)
    any_ok = (
        len(criteria.any_required) == 0
        or any(pass_by_name.get(name, False) for name in criteria.any_required)
    )
    return all_ok and any_ok


def _evaluator_rollups(
    outcomes: list[CellOutcome], variant_names: list[str]
) -> list[EvaluatorRollup]:
    by_eval: dict[str, dict[str, list[EvaluationResult]]] = {}
    for outcome in outcomes:
        for r in outcome.results:
            by_eval.setdefault(r.evaluator, {v: [] for v in variant_names})
            by_eval[r.evaluator][outcome.variant.name].append(r)

    rollups: list[EvaluatorRollup] = []
    for evaluator_name, by_variant in sorted(by_eval.items()):
        per_variant: dict[str, EvaluatorVariantRollup] = {}
        for v_name, results in by_variant.items():
            passed = sum(1 for r in results if r.passed and r.error is None)
            total = len(results)
            scores = [r.score for r in results if r.score is not None]
            per_variant[v_name] = EvaluatorVariantRollup(
                pass_rate=_safe_ratio(passed, total),
                avg_score=_avg(scores) if scores else None,
            )
        rollups.append(EvaluatorRollup(evaluator=evaluator_name, by_variant=per_variant))
    return rollups


def _safe_ratio(numer: int, denom: int) -> float:
    return 0.0 if denom == 0 else numer / denom


def _avg(values: list[float] | list[int]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)
