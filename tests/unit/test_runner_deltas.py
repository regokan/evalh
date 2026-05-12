"""Tests for the shared delta-computation primitives.

These are pure helpers — same arithmetic powers within-run variant
comparison (`ComparisonReport` kind='ad_hoc') and across-run drift
detection (kind='drift'). The tests stay at the primitive level; the
report-builder integration is covered by the existing
`test_runner.py` baseline-comparison tests.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eval_harness.core.models import (
    EvaluationResult,
    Trace,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.runner._deltas import (
    compute_evaluator_deltas,
    compute_improvements,
    compute_latency_cost_deltas,
    compute_pass_rate_delta,
    compute_regressions,
    pass_map,
)

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


def _result(
    case_id: str,
    evaluator: str = "ev1",
    *,
    passed: bool = True,
    error_type: str | None = None,
) -> EvaluationResult:
    from eval_harness.core.models import TraceError

    return EvaluationResult(
        run_id="r1",
        case_id=case_id,
        variant_name="v1",
        evaluator=evaluator,
        evaluator_type="contains_text",
        passed=passed,
        score=None,
        reason="ok",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
        error=TraceError(type=error_type, message="x") if error_type else None,
    )


def _trace(
    case_id: str,
    *,
    latency_ms: int = 10,
    cost_usd: float | None = None,
    token_input: int | None = None,
    token_output: int | None = None,
) -> Trace:
    return Trace(
        run_id="r1",
        case_id=case_id,
        variant_name="v1",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=latency_ms,
        input={},
        output=TraceOutput(final_answer="x"),
        metrics=TraceMetrics(
            cost_usd=cost_usd,
            token_input=token_input,
            token_output=token_output,
        ),
    )


# ---- pass_map ------------------------------------------------------------


def test_pass_map_all_passing() -> None:
    out = pass_map([_result("c1"), _result("c2")])
    assert out == {"c1": True, "c2": True}


def test_pass_map_one_failing_makes_cell_fail() -> None:
    """A case passes only when ALL its evaluator results pass."""
    out = pass_map([
        _result("c1", "evA", passed=True),
        _result("c1", "evB", passed=False),
        _result("c2", "evA", passed=True),
    ])
    assert out == {"c1": False, "c2": True}


def test_pass_map_error_counts_as_failure() -> None:
    out = pass_map([_result("c1", passed=True, error_type="boom")])
    assert out == {"c1": False}


def test_pass_map_empty_returns_empty() -> None:
    assert pass_map([]) == {}


# ---- pass-rate delta -----------------------------------------------------


def test_pass_rate_delta_positive_means_improvement() -> None:
    a = {"c1": False, "c2": False}
    b = {"c1": True, "c2": False}
    assert compute_pass_rate_delta(a, b) == pytest.approx(0.5)


def test_pass_rate_delta_negative_means_regression() -> None:
    a = {"c1": True, "c2": True}
    b = {"c1": True, "c2": False}
    assert compute_pass_rate_delta(a, b) == pytest.approx(-0.5)


def test_pass_rate_delta_empty_is_zero() -> None:
    assert compute_pass_rate_delta({}, {}) == 0.0


def test_pass_rate_delta_handles_disjoint_case_sets() -> None:
    """A case that exists in B but not A counts as 'fail' on the A side
    for the union — drift is measured against the same case universe."""
    a = {"c1": True}
    b = {"c1": True, "c2": True}
    # Over the union {c1, c2}: a_rate = 1/2 = 0.5; b_rate = 2/2 = 1.0
    assert compute_pass_rate_delta(a, b) == pytest.approx(0.5)


# ---- regressions / improvements ------------------------------------------


def test_regressions_lists_cases_passing_in_a_but_not_b() -> None:
    a = {"c1": True, "c2": True, "c3": False}
    b = {"c1": True, "c2": False, "c3": True}
    assert compute_regressions(a, b) == ["c2"]


def test_improvements_lists_cases_passing_in_b_but_not_a() -> None:
    a = {"c1": True, "c2": True, "c3": False}
    b = {"c1": True, "c2": False, "c3": True}
    assert compute_improvements(a, b) == ["c3"]


def test_regressions_and_improvements_are_sorted() -> None:
    """Stable ordering — webhook formatters depend on this for diffability."""
    a = {"z": True, "a": True, "m": True}
    b = {"z": False, "a": False, "m": False}
    assert compute_regressions(a, b) == ["a", "m", "z"]


def test_no_regressions_when_b_strictly_better() -> None:
    a = {"c1": False, "c2": False}
    b = {"c1": True, "c2": True}
    assert compute_regressions(a, b) == []
    assert compute_improvements(a, b) == ["c1", "c2"]


# ---- per-evaluator deltas ------------------------------------------------


def test_evaluator_deltas_separates_per_evaluator_pass_rates() -> None:
    """When `latency_under` regresses but `contains_text` holds, the
    delta map should pinpoint which evaluator drove the drift."""
    a = [
        _result("c1", "contains_text", passed=True),
        _result("c2", "contains_text", passed=True),
        _result("c1", "latency_under", passed=True),
        _result("c2", "latency_under", passed=True),
    ]
    b = [
        _result("c1", "contains_text", passed=True),
        _result("c2", "contains_text", passed=True),
        _result("c1", "latency_under", passed=False),
        _result("c2", "latency_under", passed=False),
    ]
    deltas = compute_evaluator_deltas(a, b)
    assert deltas["contains_text"] == pytest.approx(0.0)
    assert deltas["latency_under"] == pytest.approx(-1.0)


def test_evaluator_deltas_for_evaluator_only_in_b() -> None:
    """Adding a new evaluator -> delta against the missing-in-A side."""
    a = [_result("c1", "contains_text", passed=True)]
    b = [
        _result("c1", "contains_text", passed=True),
        _result("c1", "new_evaluator", passed=False),
    ]
    deltas = compute_evaluator_deltas(a, b)
    assert deltas["contains_text"] == pytest.approx(0.0)
    assert deltas["new_evaluator"] == pytest.approx(0.0)  # 0/1 - missing(0) = 0


def test_evaluator_deltas_empty_inputs() -> None:
    assert compute_evaluator_deltas([], []) == {}


def test_evaluator_deltas_treats_error_as_failure() -> None:
    a = [_result("c1", "ev", passed=True)]
    b = [_result("c1", "ev", passed=True, error_type="boom")]
    assert compute_evaluator_deltas(a, b)["ev"] == pytest.approx(-1.0)


# ---- latency / cost deltas ----------------------------------------------


def test_latency_cost_deltas_reports_averages() -> None:
    a = [
        _trace("c1", latency_ms=100, cost_usd=0.10, token_input=200, token_output=50),
        _trace("c2", latency_ms=200, cost_usd=0.20, token_input=400, token_output=100),
    ]
    b = [
        _trace("c1", latency_ms=300, cost_usd=0.30, token_input=600, token_output=150),
        _trace("c2", latency_ms=400, cost_usd=0.40, token_input=800, token_output=200),
    ]
    deltas = compute_latency_cost_deltas(a, b)
    # a avg latency = 150, b avg = 350 -> +200
    assert deltas["latency_ms"] == pytest.approx(200.0)
    # a avg cost = 0.15, b avg = 0.35 -> +0.20
    assert deltas["cost_usd"] == pytest.approx(0.20)
    assert deltas["token_input"] == pytest.approx(400.0)
    assert deltas["token_output"] == pytest.approx(100.0)


def test_latency_cost_deltas_skips_metrics_missing_on_either_side() -> None:
    """If A has no cost numbers but B does, we can't compute a delta —
    don't emit a misleading `b_avg - 0` value."""
    a = [_trace("c1", latency_ms=100)]
    b = [_trace("c1", latency_ms=150, cost_usd=0.05)]
    deltas = compute_latency_cost_deltas(a, b)
    assert deltas == {"latency_ms": 50.0}


def test_latency_cost_deltas_empty_traces() -> None:
    assert compute_latency_cost_deltas([], []) == {}
