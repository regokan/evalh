from __future__ import annotations

from typing import Any

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.models import EvalCase, FilesystemArtifact, RunVariant


class TempdirSnapshotAdapter:
    name: str

    def __init__(self, name: str = "tempdir_snapshot", **config: Any) -> None:
        self.name = name
        self._config = config

    async def prepare(self, case: EvalCase, variant: RunVariant) -> Workspace:
        raise NotImplementedError("TempdirSnapshotAdapter.prepare lands in ev-51w")

    async def collect_artifacts(self, workspace: Workspace) -> FilesystemArtifact:
        raise NotImplementedError("TempdirSnapshotAdapter.collect_artifacts lands in ev-51w")

    async def cleanup(self, workspace: Workspace) -> None:
        raise NotImplementedError("TempdirSnapshotAdapter.cleanup lands in ev-51w")
