from __future__ import annotations

from pathlib import Path

import pytest

from eval_harness.adapters.workspace.tempdir_snapshot_adapter import (
    TempdirSnapshotAdapter,
)
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import EvalCase, RunVariant


def _case(case_id: str = "c1") -> EvalCase:
    return EvalCase(id=case_id, input={"user_message": "hello"})


def _variant(name: str = "v1") -> RunVariant:
    return RunVariant(name=name, adapter="any", config={})


def test_unknown_copy_from_raises_configerror(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        TempdirSnapshotAdapter(copy_from=str(tmp_path / "does_not_exist"))


async def test_prepare_collect_modified_cleanup(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "a.txt").write_text("original line\n")
    (fixture / "subdir").mkdir()
    (fixture / "subdir" / "b.txt").write_text("keep me\n")

    adapter = TempdirSnapshotAdapter(copy_from=str(fixture))
    workspace = await adapter.prepare(_case(), _variant())

    try:
        assert workspace.path.exists()
        assert (workspace.path / "a.txt").read_text() == "original line\n"
        assert (workspace.path / "subdir" / "b.txt").read_text() == "keep me\n"

        # System modifies one file.
        (workspace.path / "a.txt").write_text("changed line\n")

        artifact = await adapter.collect_artifacts(workspace)

        assert artifact.workspace_kind == "tempdir_snapshot"
        assert artifact.case_id == "c1"
        assert artifact.variant_name == "v1"
        assert artifact.diff.modified == ["a.txt"]
        assert artifact.diff.added == []
        assert artifact.diff.removed == []
        assert artifact.before_manifest.files["a.txt"].sha256 != (
            artifact.after_manifest.files["a.txt"].sha256
        )
        # Text diff body for the modified small text file.
        assert "a.txt" in artifact.diff.text_diffs
        body = artifact.diff.text_diffs["a.txt"]
        assert "-original line" in body
        assert "+changed line" in body
    finally:
        await adapter.cleanup(workspace)

    assert not workspace.path.exists()


async def test_added_and_removed_detected(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "keep.txt").write_text("keep\n")
    (fixture / "delete_me.txt").write_text("bye\n")

    adapter = TempdirSnapshotAdapter(copy_from=str(fixture))
    workspace = await adapter.prepare(_case(), _variant())
    try:
        (workspace.path / "delete_me.txt").unlink()
        (workspace.path / "new.txt").write_text("hello\n")

        artifact = await adapter.collect_artifacts(workspace)
        assert artifact.diff.added == ["new.txt"]
        assert artifact.diff.removed == ["delete_me.txt"]
        assert artifact.diff.modified == []
        assert "new.txt" in artifact.diff.text_diffs
        assert "+hello" in artifact.diff.text_diffs["new.txt"]
    finally:
        await adapter.cleanup(workspace)


async def test_binary_modified_no_text_diff_body(tmp_path: Path) -> None:
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    (fixture / "blob.bin").write_bytes(b"\x00\x01\x02\x03" * 32)

    adapter = TempdirSnapshotAdapter(copy_from=str(fixture))
    workspace = await adapter.prepare(_case(), _variant())
    try:
        (workspace.path / "blob.bin").write_bytes(b"\x00\xff\xff\xff" * 32)
        artifact = await adapter.collect_artifacts(workspace)
        assert artifact.diff.modified == ["blob.bin"]
        assert "blob.bin" not in artifact.diff.text_diffs
    finally:
        await adapter.cleanup(workspace)


async def test_prepare_without_copy_from_yields_empty_workspace(tmp_path: Path) -> None:
    adapter = TempdirSnapshotAdapter(base_path=str(tmp_path))
    workspace = await adapter.prepare(_case(), _variant())
    try:
        assert workspace.path.exists()
        assert list(workspace.path.iterdir()) == []
        artifact = await adapter.collect_artifacts(workspace)
        assert artifact.before_manifest.files == {}
        assert artifact.after_manifest.files == {}
        assert artifact.diff.added == []
        assert artifact.diff.removed == []
        assert artifact.diff.modified == []
    finally:
        await adapter.cleanup(workspace)


async def test_collect_artifacts_without_prepare_raises() -> None:
    from eval_harness.adapters.workspace.base import Workspace

    adapter = TempdirSnapshotAdapter()
    ws = Workspace(path=Path("/tmp/does_not_matter"), metadata={})
    with pytest.raises(AdapterError):
        await adapter.collect_artifacts(ws)


def test_workspace_factory_registers_tempdir_snapshot() -> None:
    from eval_harness.factories import workspace_factory

    assert "tempdir_snapshot" in workspace_factory.registry.names()
