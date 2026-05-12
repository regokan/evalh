from __future__ import annotations

from eval_harness.core.models import (
    ComparisonReport,
    RunVariant,
    VariantDelta,
    VariantSummary,
)


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
        regressions = sorted(
            case_id for case_id, passed in base_pass.items()
            if passed and not other_pass.get(case_id, False)
        )
        improvements = sorted(
            case_id for case_id, passed in other_pass.items()
            if passed and not base_pass.get(case_id, False)
        )
        deltas.append(
            VariantDelta(
                variant=variant.name,
                pass_rate_delta=other.pass_rate - base.pass_rate,
                avg_latency_delta_ms=other.avg_latency_ms - base.avg_latency_ms,
                regressions=regressions,
                improvements=improvements,
            )
        )
    return ComparisonReport(baseline=baseline, deltas=deltas)
