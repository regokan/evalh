from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Self

from eval_harness.core.models import (
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    Trace,
)


class LocalFilesStore:
    def __init__(self, **config: Any) -> None:
        self._config = config

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def open(self, run_id: str, run_dir: Path) -> None:
        raise NotImplementedError("LocalFilesStore.open lands in ev-j78")

    async def save_trace(self, trace: Trace) -> None:
        raise NotImplementedError("LocalFilesStore.save_trace lands in ev-j78")

    async def save_evaluation(
        self,
        case_id: str,
        variant: str,
        results: list[EvaluationResult],
    ) -> None:
        raise NotImplementedError("LocalFilesStore.save_evaluation lands in ev-j78")

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        raise NotImplementedError("LocalFilesStore.save_artifact lands in ev-j78")

    async def save_summary(self, summary: RunSummary) -> None:
        raise NotImplementedError("LocalFilesStore.save_summary lands in ev-j78")

    async def close(self) -> None:
        raise NotImplementedError("LocalFilesStore.close lands in ev-j78")
