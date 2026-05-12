from __future__ import annotations

from typing import TYPE_CHECKING

from eval_harness.core.models import RunSummary

if TYPE_CHECKING:
    from eval_harness.adapters.trace.base import TraceStore


async def write_summary(store: TraceStore, summary: RunSummary) -> None:
    await store.save_summary(summary)
