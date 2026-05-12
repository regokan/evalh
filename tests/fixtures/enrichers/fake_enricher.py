"""Programmable TraceEnricher used by enrichment tests.

`should_raise` -> enrich() raises a deterministic RuntimeError (exercises
failure-soft).  `delay_ticks` -> enrich() yields N event-loop turns before
returning (cheap proxy for ingestion-lag retries without hitting wall
time).  `enriched_fields` -> merged into `trace.extra` so tests can
assert downstream visibility.
"""

from __future__ import annotations

import asyncio
from types import TracebackType
from typing import Any, Self

from eval_harness.core.models import Trace


class FakeEnricher:
    """Test enricher: deterministic, side-effect-recording."""

    def __init__(
        self,
        name: str = "fake",
        *,
        should_raise: bool = False,
        delay_ticks: int = 0,
        enriched_fields: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        self.name = name
        self.should_raise = should_raise
        self.delay_ticks = delay_ticks
        self.enriched_fields: dict[str, Any] = dict(enriched_fields or {})
        self.call_count: int = 0
        self.entered: bool = False
        self.exited: bool = False

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.exited = True

    async def enrich(self, trace: Trace) -> Trace:
        self.call_count += 1
        for _ in range(self.delay_ticks):
            await asyncio.sleep(0)
        if self.should_raise:
            raise RuntimeError(f"fake-enricher {self.name!r} configured to raise")
        # Record what this enricher saw and what it added so chain-order
        # tests can introspect.
        trail = trace.extra.setdefault("enriched_by", [])
        trail.append(self.name)
        if self.enriched_fields:
            target = trace.extra.setdefault("enrichment", {})
            target.update(self.enriched_fields)
        return trace
