"""TraceEnricher Protocol — the sixth adapter family.

Enrichers run AFTER a SystemAdapter returns a Trace and BEFORE evaluators
see it. Typical job: fetch the matching upstream trace from Langfuse /
Phoenix / Arize / OTel and merge richer fields onto our local Trace.

**Failure-soft semantics are load-bearing.** An enricher that times out
or errors must NOT fail the cell. The runner catches exceptions, appends
``{enricher: name, error: ...}`` to ``trace.extra.enrichment_errors``,
and proceeds with the un-enriched trace. Production observability
hiccups stay out of the eval's pass/fail decision.

See ``docs/Adapters.md`` > "TraceEnricher".
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from eval_harness.core.models import Trace


@runtime_checkable
class TraceEnricher(Protocol):
    """Lifecycle + one ``enrich`` call per Trace.

    Concrete enrichers may mutate the passed-in Trace and return it, or
    return a new Trace — callers must use the return value either way.
    """

    name: str

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def enrich(self, trace: Trace) -> Trace: ...
