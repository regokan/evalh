from __future__ import annotations

from eval_harness.adapters.enricher.base import TraceEnricher
from eval_harness.factories import trace_enricher_factory

trace_enricher_factory.load_entry_points()

__all__ = ["TraceEnricher"]
