from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
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


@dataclass
class _VariantAcc:
    """Fixed-size accumulator per variant.

    Holds running counters + sums only — memory does NOT grow with case count.
    """

    cases_total: int = 0
    cases_passed: int = 0
    cases_errored: int = 0
    sum_latency: int = 0
    sum_cost: float = 0.0
    cost_count: int = 0
    sum_tokens_in: int = 0
    tokens_in_count: int = 0
    sum_tokens_out: int = 0
    tokens_out_count: int = 0


@dataclass
class _EvalAcc:
    """Fixed-size accumulator per (evaluator, variant)."""

    total: int = 0
    passed: int = 0
    score_sum: float = 0.0
    score_count: int = 0


@dataclass
class SummaryAggregator:
    """Single-pass aggregator that turns a stream of CellOutcomes into a
    RunSummary without ever holding all of them in memory at once.

    Per-case state is limited to `case_pass_by_variant` (one bool per
    (variant, case) — needed for the per-case comparison report); everything
    else is fixed-size counters. For 10K cases x 3 variants this is ~30K bools
    plus a constant amount of accumulator state, well under the 1GB perf budget.
    """

    plan: RunPlan
    _variants: dict[str, _VariantAcc] = field(init=False)
    _case_pass: dict[str, dict[str, bool]] = field(init=False)
    _evals: dict[tuple[str, str], _EvalAcc] = field(init=False)
    _eval_names: list[str] = field(init=False)

    def __post_init__(self) -> None:
        self._variants = {v.name: _VariantAcc() for v in self.plan.variants}
        self._case_pass = {v.name: {} for v in self.plan.variants}
        self._evals = {}
        self._eval_names = []

    def add(self, outcome: CellOutcome) -> None:
        v_name = outcome.variant.name
        va = self._variants[v_name]
        passed = _case_passes(outcome.results, self.plan.config.pass_criteria)
        self._case_pass[v_name][outcome.case.id] = passed

        va.cases_total += 1
        if passed:
            va.cases_passed += 1
        if outcome.trace.error is not None:
            va.cases_errored += 1
        va.sum_latency += outcome.trace.latency_ms

        metrics = outcome.trace.metrics
        if metrics.cost_usd is not None:
            va.sum_cost += metrics.cost_usd
            va.cost_count += 1
        if metrics.token_input is not None:
            va.sum_tokens_in += metrics.token_input
            va.tokens_in_count += 1
        if metrics.token_output is not None:
            va.sum_tokens_out += metrics.token_output
            va.tokens_out_count += 1

        for r in outcome.results:
            key = (r.evaluator, v_name)
            ea = self._evals.get(key)
            if ea is None:
                ea = _EvalAcc()
                self._evals[key] = ea
                if r.evaluator not in self._eval_names:
                    self._eval_names.append(r.evaluator)
            ea.total += 1
            if r.passed and r.error is None:
                ea.passed += 1
            if r.score is not None:
                ea.score_sum += r.score
                ea.score_count += 1

    def finalize(self) -> RunSummary:
        variant_summaries: list[VariantSummary] = []
        for variant in self.plan.variants:
            va = self._variants[variant.name]
            variant_summaries.append(
                VariantSummary(
                    name=variant.name,
                    cases_total=va.cases_total,
                    cases_passed=va.cases_passed,
                    cases_errored=va.cases_errored,
                    pass_rate=_safe_ratio(va.cases_passed, va.cases_total),
                    avg_latency_ms=_safe_avg(va.sum_latency, va.cases_total),
                    avg_cost_usd=_avg_or_none(va.sum_cost, va.cost_count),
                    avg_tokens_input=_avg_or_none(va.sum_tokens_in, va.tokens_in_count),
                    avg_tokens_output=_avg_or_none(va.sum_tokens_out, va.tokens_out_count),
                )
            )

        rollups: list[EvaluatorRollup] = []
        variant_names = [v.name for v in self.plan.variants]
        for ev_name in sorted(self._eval_names):
            per_variant: dict[str, EvaluatorVariantRollup] = {}
            for v_name in variant_names:
                ea = self._evals.get((ev_name, v_name))
                if ea is None:
                    per_variant[v_name] = EvaluatorVariantRollup(
                        pass_rate=0.0, avg_score=None
                    )
                    continue
                per_variant[v_name] = EvaluatorVariantRollup(
                    pass_rate=_safe_ratio(ea.passed, ea.total),
                    avg_score=_avg_or_none(ea.score_sum, ea.score_count),
                )
            rollups.append(
                EvaluatorRollup(evaluator=ev_name, by_variant=per_variant)
            )

        comparison: ComparisonReport | None = None
        if self.plan.baseline_variant is not None:
            comparison = build_comparison(
                baseline=self.plan.baseline_variant,
                variants=self.plan.variants,
                variant_summaries=variant_summaries,
                case_pass_by_variant=self._case_pass,
            )

        now = utc_now()
        return RunSummary(
            run_id=self.plan.run_id,
            started_at=now,
            finished_at=now,
            config_path=str(self.plan.run_dir),
            config_hash="",
            cases_total=len(self.plan.cases),
            variants=variant_summaries,
            by_evaluator=rollups,
            comparison=comparison,
        )


def build_summary(outcomes: Iterable[CellOutcome], plan: RunPlan) -> RunSummary:
    """Single-pass streaming aggregation; does NOT materialise `outcomes`.

    Kept as a free function for backward compatibility with tests that already
    pass a list. Internal callers prefer `SummaryAggregator.add(...).finalize()`
    so they never buffer outcomes at all.
    """
    agg = SummaryAggregator(plan=plan)
    for outcome in outcomes:
        agg.add(outcome)
    return agg.finalize()


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


def _safe_ratio(numer: int, denom: int) -> float:
    return 0.0 if denom == 0 else numer / denom


def _safe_avg(total: int, count: int) -> float:
    return 0.0 if count == 0 else total / count


def _avg_or_none(total: float, count: int) -> float | None:
    return None if count == 0 else total / count
