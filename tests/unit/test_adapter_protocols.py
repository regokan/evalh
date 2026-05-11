from __future__ import annotations


def test_system_adapter_protocol_importable() -> None:
    from eval_harness.adapters.system.base import SystemAdapter

    assert SystemAdapter.__name__ == "SystemAdapter"


def test_dataset_adapter_protocol_importable() -> None:
    from eval_harness.adapters.dataset.base import DatasetAdapter

    assert DatasetAdapter.__name__ == "DatasetAdapter"


def test_trace_store_protocol_importable() -> None:
    from eval_harness.adapters.trace.base import TraceStore

    assert TraceStore.__name__ == "TraceStore"


def test_workspace_adapter_protocol_and_model_importable() -> None:
    from eval_harness.adapters.workspace.base import Workspace, WorkspaceAdapter

    assert WorkspaceAdapter.__name__ == "WorkspaceAdapter"
    assert Workspace.__name__ == "Workspace"


def test_evaluator_base_importable() -> None:
    from eval_harness.evaluators.base import Evaluator

    assert Evaluator.__name__ == "Evaluator"
    # The class-level `type` attribute exists.
    assert hasattr(Evaluator, "type")


def test_workspace_model_constructs() -> None:
    from pathlib import Path

    from eval_harness.adapters.workspace.base import Workspace

    w = Workspace(path=Path("/tmp/x"), metadata={"k": "v"})
    assert w.path == Path("/tmp/x")
    assert w.metadata == {"k": "v"}
