from __future__ import annotations

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

    async def save_evaluation(
        self,
        case_id: str,
        variant: str,
        results: list[EvaluationResult],
    ) -> None: ...

    async def save_artifact(self, artifact: FilesystemArtifact) -> None: ...

    async def save_summary(self, summary: RunSummary) -> None: ...

    async def close(self) -> None: ...
