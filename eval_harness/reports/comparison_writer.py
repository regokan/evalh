"""Within-run variant comparison.

Produces a `ComparisonReport` with ``kind='ad_hoc'`` from a single run's
variants + evaluator results. Drift comparisons (run A vs run B) share
the per-case-pass-rate arithmetic via `eval_harness.runner._deltas`; this
file just shapes the inputs for the within-run case.
"""

from __future__ import annotations

from eval_harness.core.models import (
    ComparisonReport,
    RunVariant,
    VariantDelta,
    VariantSummary,
)
from eval_harness.runner._deltas import compute_improvements, compute_regressions


def build_comparison(
    *,
    baseline: str,
    variants: list[RunVariant],
    variant_summaries: list[VariantSummary],
    case_pass_by_variant: dict[str, dict[str, bool]],
) -> ComparisonReport:
    summary_by_name = {s.name: s for s in variant_summaries}
    base = summary_by_name.get(baseline)
    if base is None:
        return ComparisonReport(baseline=baseline, deltas=[])

    base_pass = case_pass_by_variant.get(baseline, {})

    deltas: list[VariantDelta] = []
    for variant in variants:
        if variant.name == baseline:
            continue
        other = summary_by_name.get(variant.name)
        if other is None:
            continue
        other_pass = case_pass_by_variant.get(variant.name, {})
        deltas.append(
            VariantDelta(
                variant=variant.name,
                pass_rate_delta=other.pass_rate - base.pass_rate,
                avg_latency_delta_ms=other.avg_latency_ms - base.avg_latency_ms,
                regressions=compute_regressions(base_pass, other_pass),
                improvements=compute_improvements(base_pass, other_pass),
            )
        )
    return ComparisonReport(baseline=baseline, deltas=deltas, kind="ad_hoc")
