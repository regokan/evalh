from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from eval_harness.core.models import (
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    Trace,
)


@runtime_checkable
class TraceStore(Protocol):
    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def open(self, run_id: str, run_dir: Path) -> None: ...

    async def save_trace(self, trace: Trace) -> None: ...

    async def save_trace_idempotent(self, trace: Trace, cell_id: str) -> bool:
        """v2 idempotency contract — keyed by deterministic `cell_id`.

        Returns True when the trace was written (or overwrites an existing
        error-state record on retry), False when an existing successful
        record for `cell_id` was found and the call was a no-op. The
        canonical sinks (local_files, sqlite, postgres) provide real
        implementations; other stores inherit the always-write fallback
        (the idempotency contract is enforced at the canonical sink — see
        ``docs/Adapters.md`` > "Trace store idempotency")."""
        ...

    async def save_evaluation(
        self,
        case_id: str,
        variant: str,
        results: list[EvaluationResult],
    ) -> None: ...

    async def save_artifact(self, artifact: FilesystemArtifact) -> None: ...

    async def save_summary(self, summary: RunSummary) -> None: ...

    async def close(self) -> None: ...

    # ---- Read methods (v0.2). All backends must implement these so callers
    # (RunReader, evalh inspect/compare/re-evaluate) work uniformly across
    # local_files / sqlite / postgres. ``run_id=None`` streams every run the
    # backend knows about; ``batch_size`` is a hint — backends may fetch in
    # bigger chunks. Callers must not assume any batch boundary.

    def iter_traces(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[Trace]: ...

    def iter_results(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[EvaluationResult]: ...

    async def load_summary(self, run_id: str) -> RunSummary | None: ...

    async def list_run_ids(self) -> list[str]: ...
