"""Shared delta-computation primitives.

Both within-run variant comparisons (``ComparisonReport`` with
``kind='ad_hoc'``, owned by ``eval_harness.reports.comparison_writer``)
and across-run drift reports (``kind='drift'``, owned by the upcoming
drift CLI / webhook sink) need the same arithmetic: pass-rate deltas,
regression / improvement case lists, per-evaluator deltas, and
latency / cost deltas.

This module owns those primitives as pure functions. Callers shape the
input the way that makes sense for their context (variant-within-run vs
baseline-run-vs-current-run); the math is the same either way.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from eval_harness.core.models import EvaluationResult, Trace

T = TypeVar("T")


def pass_map(results: Iterable[EvaluationResult]) -> dict[str, bool]:
    """Reduce an EvaluationResult stream to ``{case_id: passed}``.

    A case "passes" iff every result for it passes (error counts as a
    failure). Order-independent so callers can stream results from any
    backend.
    """
    out: dict[str, bool] = {}
    for r in results:
        prior = out.get(r.case_id)
        cell_passed = bool(r.passed) and r.error is None
        if prior is None:
            out[r.case_id] = cell_passed
        else:
            out[r.case_id] = prior and cell_passed
    return out


def compute_pass_rate_delta(
    a_pass: dict[str, bool],
    b_pass: dict[str, bool],
) -> float:
    """`b.pass_rate - a.pass_rate` over the union of case_ids."""
    union = set(a_pass) | set(b_pass)
    if not union:
        return 0.0
    a_rate = sum(1 for c in union if a_pass.get(c, False)) / len(union)
    b_rate = sum(1 for c in union if b_pass.get(c, False)) / len(union)
    return b_rate - a_rate


def compute_regressions(
    a_pass: dict[str, bool],
    b_pass: dict[str, bool],
) -> list[str]:
    """Cases that passed in A but did not pass in B. Sorted for stability."""
    return sorted(
        case_id
        for case_id, passed in a_pass.items()
        if passed and not b_pass.get(case_id, False)
    )


def compute_improvements(
    a_pass: dict[str, bool],
    b_pass: dict[str, bool],
) -> list[str]:
    """Cases that passed in B but did not pass in A. Sorted for stability."""
    return sorted(
        case_id
        for case_id, passed in b_pass.items()
        if passed and not a_pass.get(case_id, False)
    )


def compute_evaluator_deltas(
    a_results: Iterable[EvaluationResult],
    b_results: Iterable[EvaluationResult],
) -> dict[str, float]:
    """Per-evaluator pass-rate delta: ``b_rate - a_rate`` keyed by
    evaluator name. Useful for spotting "the latency_under evaluator is
    the one driving the regression" rather than just per-case verdicts.
    """
    a_by_eval = _pass_rate_by_evaluator(a_results)
    b_by_eval = _pass_rate_by_evaluator(b_results)
    out: dict[str, float] = {}
    for name in set(a_by_eval) | set(b_by_eval):
        out[name] = b_by_eval.get(name, 0.0) - a_by_eval.get(name, 0.0)
    return out


def _pass_rate_by_evaluator(
    results: Iterable[EvaluationResult],
) -> dict[str, float]:
    totals: dict[str, int] = {}
    passes: dict[str, int] = {}
    for r in results:
        totals[r.evaluator] = totals.get(r.evaluator, 0) + 1
        if r.passed and r.error is None:
            passes[r.evaluator] = passes.get(r.evaluator, 0) + 1
    return {name: passes.get(name, 0) / total for name, total in totals.items() if total}


def compute_latency_cost_deltas(
    a_traces: Iterable[Trace],
    b_traces: Iterable[Trace],
) -> dict[str, float]:
    """Average-trace delta for the latency + cost metrics that move under
    drift. Returns ``{metric_name: b_avg - a_avg}``; metrics with no data
    on either side are omitted (no `None` minus number).
    """
    a_avgs = _trace_averages(a_traces)
    b_avgs = _trace_averages(b_traces)
    out: dict[str, float] = {}
    for metric in {"latency_ms", "cost_usd", "token_input", "token_output"}:
        if metric in a_avgs and metric in b_avgs:
            out[metric] = b_avgs[metric] - a_avgs[metric]
    return out


def _trace_averages(traces: Iterable[Trace]) -> dict[str, float]:
    latency_total = 0.0
    latency_count = 0
    cost_total = 0.0
    cost_count = 0
    tin_total = 0
    tin_count = 0
    tout_total = 0
    tout_count = 0
    for t in traces:
        latency_total += float(t.latency_ms)
        latency_count += 1
        if t.metrics.cost_usd is not None:
            cost_total += float(t.metrics.cost_usd)
            cost_count += 1
        if t.metrics.token_input is not None:
            tin_total += int(t.metrics.token_input)
            tin_count += 1
        if t.metrics.token_output is not None:
            tout_total += int(t.metrics.token_output)
            tout_count += 1
    out: dict[str, float] = {}
    if latency_count:
        out["latency_ms"] = latency_total / latency_count
    if cost_count:
        out["cost_usd"] = cost_total / cost_count
    if tin_count:
        out["token_input"] = tin_total / tin_count
    if tout_count:
        out["token_output"] = tout_total / tout_count
    return out


__all__ = [
    "compute_evaluator_deltas",
    "compute_improvements",
    "compute_latency_cost_deltas",
    "compute_pass_rate_delta",
    "compute_regressions",
    "pass_map",
]
