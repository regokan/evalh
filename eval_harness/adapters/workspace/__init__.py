from __future__ import annotations

from eval_harness.adapters.workspace.base import Workspace, WorkspaceAdapter
from eval_harness.factories import workspace_factory

workspace_factory.load_entry_points()

__all__ = ["Workspace", "WorkspaceAdapter"]
