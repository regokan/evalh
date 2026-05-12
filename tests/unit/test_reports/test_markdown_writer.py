from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from eval_harness.core.models import (
    ComparisonReport,
    EvaluatorRollup,
    EvaluatorVariantRollup,
    RunSummary,
    VariantDelta,
    VariantSummary,
)
from eval_harness.reports.markdown_writer import write_markdown_report


def _variant(
    name: str = "v1",
    *,
    passed: int = 8,
    total: int = 10,
    errored: int = 0,
    avg_latency_ms: float = 350.0,
    avg_cost_usd: float | None = 0.0123,
    avg_tokens_input: float | None = 120.0,
    avg_tokens_output: float | None = 90.0,
) -> VariantSummary:
    return VariantSummary(
        name=name,
        cases_total=total,
        cases_passed=passed,
        cases_errored=errored,
        pass_rate=passed / total if total else 0.0,
        avg_latency_ms=avg_latency_ms,
        avg_cost_usd=avg_cost_usd,
        avg_tokens_input=avg_tokens_input,
        avg_tokens_output=avg_tokens_output,
    )


def _summary(
    variants: list[VariantSummary],
    *,
    comparison: ComparisonReport | None = None,
    by_evaluator: list[EvaluatorRollup] | None = None,
) -> RunSummary:
    return RunSummary(
        run_id="2026-05-12T10-00-00_my_eval",
        started_at=datetime(2026, 5, 12, 10, 0, 0, tzinfo=UTC),
        finished_at=datetime(2026, 5, 12, 10, 0, 12, tzinfo=UTC),
        config_path="examples/listing_price/eval.yaml",
        config_hash="abc123",
        cases_total=variants[0].cases_total if variants else 0,
        variants=variants,
        by_evaluator=by_evaluator or [],
        comparison=comparison,
    )


def test_writes_report_with_variant_table(tmp_path: Path) -> None:
    summary = _summary([_variant("agent_main"), _variant("agent_experimental", passed=6)])
    out = write_markdown_report(tmp_path, summary)
    assert out == tmp_path / "report.md"
    text = out.read_text()
    assert "# Eval run — `2026-05-12T10-00-00_my_eval`" in text
    assert "examples/listing_price/eval.yaml" in text
    assert "## Variants" in text
    # Header row.
    assert "| Variant | Pass rate |" in text
    # Both variants present.
    assert "`agent_main`" in text
    assert "`agent_experimental`" in text
    assert "80.0%" in text
    assert "60.0%" in text
    # Numbers rendered.
    assert "350" in text


def test_writes_to_explicit_out_path(tmp_path: Path) -> None:
    summary = _summary([_variant()])
    custom = tmp_path / "custom" / "out.md"
    written = write_markdown_report(tmp_path, summary, out_path=custom)
    assert written == custom
    assert custom.exists()


def test_renders_comparison_section_when_baseline_set(tmp_path: Path) -> None:
    summary = _summary(
        [_variant("agent_main"), _variant("agent_experimental", passed=6)],
        comparison=ComparisonReport(
            baseline="agent_main",
            deltas=[
                VariantDelta(
                    variant="agent_experimental",
                    pass_rate_delta=-0.2,
                    avg_latency_delta_ms=80.0,
                    regressions=["case_001", "case_007"],
                    improvements=["case_003"],
                )
            ],
        ),
    )
    text = write_markdown_report(tmp_path, summary).read_text()
    assert "## Comparison" in text
    assert "Baseline: `agent_main`" in text
    assert "-20.0pp" in text
    assert "+80" in text
    assert "**Regressions (2):**" in text
    assert "- `case_001`" in text
    assert "- `case_007`" in text
    assert "**Improvements (1):**" in text
    assert "- `case_003`" in text


def test_skips_comparison_when_no_baseline(tmp_path: Path) -> None:
    summary = _summary([_variant()])
    text = write_markdown_report(tmp_path, summary).read_text()
    assert "## Comparison" not in text
    assert "Baseline:" not in text


def test_renders_evaluator_rollup(tmp_path: Path) -> None:
    summary = _summary(
        [_variant("agent_main"), _variant("agent_experimental")],
        by_evaluator=[
            EvaluatorRollup(
                evaluator="must_call_listing_tool",
                by_variant={
                    "agent_main": EvaluatorVariantRollup(pass_rate=1.0, avg_score=None),
                    "agent_experimental": EvaluatorVariantRollup(
                        pass_rate=0.7, avg_score=0.85
                    ),
                },
            ),
            EvaluatorRollup(
                evaluator="answer_quality",
                by_variant={
                    "agent_main": EvaluatorVariantRollup(pass_rate=0.9, avg_score=0.92),
                },
            ),
        ],
    )
    text = write_markdown_report(tmp_path, summary).read_text()
    assert "## Per-evaluator pass rates" in text
    assert "`must_call_listing_tool`" in text
    assert "`answer_quality`" in text
    assert "100.0%" in text
    assert "70.0%" in text
    # Missing variant rendered as em-dash.
    assert "—" in text


def test_handles_zero_variants(tmp_path: Path) -> None:
    summary = _summary([])
    text = write_markdown_report(tmp_path, summary).read_text()
    assert "## Variants" in text
    # Table header is present but no data rows.
    assert "| Variant | Pass rate |" in text


def test_handles_none_numeric_fields(tmp_path: Path) -> None:
    summary = _summary(
        [
            _variant(
                "agent_main",
                avg_cost_usd=None,
                avg_tokens_input=None,
                avg_tokens_output=None,
            )
        ]
    )
    text = write_markdown_report(tmp_path, summary).read_text()
    # Em-dash placeholder for missing values; no Python "None" leakage.
    assert "None" not in text
    assert "—" in text


def test_empty_deltas_comparison_renders_note(tmp_path: Path) -> None:
    summary = _summary(
        [_variant()],
        comparison=ComparisonReport(baseline="v1", deltas=[]),
    )
    text = write_markdown_report(tmp_path, summary).read_text()
    assert "## Comparison" in text
    assert "No non-baseline variants to compare" in text
