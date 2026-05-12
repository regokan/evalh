"""Run-level cost accumulator for the v0.2 cost guardrail.

Single-threaded usage from the runner: tally happens on the runner's event
loop after each cell completes; check_limit is consulted before dispatching
a new cell. The guard is a SOFT one — cells already in flight finish
naturally; only un-started cells are short-circuited with a
``cost_limit``-typed Trace.
"""

from __future__ import annotations

from eval_harness.core.models import Trace


class CostAccumulator:
    def __init__(self) -> None:
        self._total_usd: float = 0.0

    def tally(self, trace: Trace) -> None:
        """Add ``trace.metrics.cost_usd`` to the running total.

        Traces with no recorded cost (``cost_usd is None``) contribute zero —
        e.g. adapters that don't report cost, or short-circuited cost_limit
        traces themselves.
        """
        cost = trace.metrics.cost_usd
        if cost is not None:
            self._total_usd += float(cost)

    def total_usd(self) -> float:
        return self._total_usd

    def check_limit(self, limit_usd: float | None) -> bool:
        """Return True when the accumulated total has crossed ``limit_usd``.

        ``None`` disables the guard (always returns False).
        """
        if limit_usd is None:
            return False
        return self._total_usd >= limit_usd
