from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pygit2")

from eval_harness.adapters.workspace.git_workspace_adapter import GitWorkspaceAdapter
from eval_harness.adapters.workspace.tempdir_snapshot_adapter import (
    TempdirSnapshotAdapter,
)
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import EvalCase, FilesystemArtifact, RunVariant


def _case(case_id: str = "c1") -> EvalCase:
    return EvalCase(id=case_id, input={"user_message": "hi"})


def _variant(name: str = "v1") -> RunVariant:
    return RunVariant(name=name, adapter="any", config={})


def _make_fixture(root: Path) -> Path:
    fixture = root / "fixture"
    fixture.mkdir()
    (fixture / "a.txt").write_text("original line\n")
    (fixture / "subdir").mkdir()
    (fixture / "subdir" / "b.txt").write_text("keep me\n")
    return fixture


async def test_prepare_initializes_git_if_missing(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    adapter = GitWorkspaceAdapter(copy_from=str(fixture))
    workspace = await adapter.prepare(_case(), _variant())
    try:
        assert (workspace.path / ".git").exists()
        sha = workspace.metadata["git_before"]
        assert isinstance(sha, str) and len(sha) >= 7
        assert workspace.metadata["git_initial"] is True
        assert workspace.metadata["git_branch"]  # master or main
        # File contents copied through.
        assert (workspace.path / "a.txt").read_text() == "original line\n"
    finally:
        await adapter.cleanup(workspace)
    assert not workspace.path.exists()


async def test_collect_artifacts_uses_git_diff(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    adapter = GitWorkspaceAdapter(copy_from=str(fixture))
    workspace = await adapter.prepare(_case(), _variant())
    try:
        # System modifies, adds, and deletes.
        (workspace.path / "a.txt").write_text("changed line\n")
        (workspace.path / "new.txt").write_text("brand new\n")
        (workspace.path / "subdir" / "b.txt").unlink()

        artifact = await adapter.collect_artifacts(workspace)
        assert artifact.workspace_kind == "git"
        assert artifact.diff.modified == ["a.txt"]
        assert artifact.diff.added == ["new.txt"]
        assert artifact.diff.removed == ["subdir/b.txt"]
        # Git produces unified-diff bodies.
        body_a = artifact.diff.text_diffs["a.txt"]
        assert "-original line" in body_a
        assert "+changed line" in body_a
        assert body_a.startswith("diff --git a/a.txt b/a.txt")
        # Added file body present.
        assert "+brand new" in artifact.diff.text_diffs["new.txt"]
    finally:
        await adapter.cleanup(workspace)


async def test_cleanup_removes_tempdir(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    adapter = GitWorkspaceAdapter(copy_from=str(fixture))
    workspace = await adapter.prepare(_case(), _variant())
    assert workspace.path.exists()
    await adapter.cleanup(workspace)
    assert not workspace.path.exists()


async def test_baseline_sha_present_in_metadata(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    adapter = GitWorkspaceAdapter(copy_from=str(fixture))
    workspace = await adapter.prepare(_case(), _variant())
    try:
        # This is the value-add over tempdir_snapshot.
        sha = workspace.metadata["git_before"]
        assert isinstance(sha, str)
        # Real OIDs are 40 hex chars.
        assert len(sha) == 40
        int(sha, 16)
    finally:
        await adapter.cleanup(workspace)


async def test_contract_matches_tempdir_snapshot_shape(tmp_path: Path) -> None:
    """Both adapters must produce a FilesystemArtifact with the same schema and
    the same overall diff *categories* for the same modifications. Text-diff
    bodies differ (difflib vs git), but added/removed/modified lists agree."""
    fixture = _make_fixture(tmp_path)

    async def run(adapter: TempdirSnapshotAdapter | GitWorkspaceAdapter) -> FilesystemArtifact:
        ws = await adapter.prepare(_case(), _variant())
        try:
            (ws.path / "a.txt").write_text("changed\n")
            (ws.path / "added.txt").write_text("hi\n")
            (ws.path / "subdir" / "b.txt").unlink()
            return await adapter.collect_artifacts(ws)
        finally:
            await adapter.cleanup(ws)

    a_temp = await run(TempdirSnapshotAdapter(copy_from=str(fixture)))
    a_git = await run(GitWorkspaceAdapter(copy_from=str(fixture)))

    # Identical pydantic schema.
    assert type(a_temp) is type(a_git) is FilesystemArtifact
    assert set(FilesystemArtifact.model_fields.keys()) == {
        "schema_version",
        "case_id",
        "variant_name",
        "workspace_kind",
        "before_manifest",
        "after_manifest",
        "diff",
        "artifacts_path",
    }

    # FileManifest entry keys agree before the modification (same fixture).
    assert set(a_temp.before_manifest.files) == set(a_git.before_manifest.files)
    for path in a_temp.before_manifest.files:
        # Same sha256 regardless of workspace kind.
        assert (
            a_temp.before_manifest.files[path].sha256
            == a_git.before_manifest.files[path].sha256
        )

    # Diff categorization agrees.
    assert sorted(a_temp.diff.added) == sorted(a_git.diff.added)
    assert sorted(a_temp.diff.removed) == sorted(a_git.diff.removed)
    assert sorted(a_temp.diff.modified) == sorted(a_git.diff.modified)

    # Both produce some text-diff body for the modified file (bodies differ).
    assert "a.txt" in a_temp.diff.text_diffs
    assert "a.txt" in a_git.diff.text_diffs


async def test_init_git_false_without_existing_repo_raises(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    adapter = GitWorkspaceAdapter(copy_from=str(fixture), init_git=False)
    with pytest.raises(ConfigError):
        await adapter.prepare(_case(), _variant())


async def test_collect_without_prepare_raises(tmp_path: Path) -> None:
    from eval_harness.adapters.workspace.base import Workspace

    adapter = GitWorkspaceAdapter()
    ws = Workspace(path=tmp_path, metadata={})
    with pytest.raises(AdapterError):
        await adapter.collect_artifacts(ws)


def test_unknown_copy_from_raises_configerror(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        GitWorkspaceAdapter(copy_from=str(tmp_path / "does_not_exist"))


def test_factory_registers_git_workspace() -> None:
    from eval_harness.factories import workspace_factory

    assert "git" in workspace_factory.registry.names()
