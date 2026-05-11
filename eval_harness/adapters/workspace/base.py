from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from eval_harness.core.models import EvalCase, FilesystemArtifact, RunVariant


class Workspace(BaseModel):
    path: Path
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class WorkspaceAdapter(Protocol):
    name: str

    async def prepare(self, case: EvalCase, variant: RunVariant) -> Workspace: ...

    async def collect_artifacts(self, workspace: Workspace) -> FilesystemArtifact: ...

    async def cleanup(self, workspace: Workspace) -> None: ...
