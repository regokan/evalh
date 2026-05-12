"""DockerVolumeAdapter tests — all gated by @pytest.mark.docker.

CI's pytest invocation pairs this marker with a `command -v docker` step so
that when docker is expected but unavailable the test FAILS LOUDLY rather
than silently skipping (per ev-yry).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from eval_harness.adapters.workspace.docker_volume_adapter import (
    DockerVolumeAdapter,
)
from eval_harness.adapters.workspace.tempdir_snapshot_adapter import (
    TempdirSnapshotAdapter,
)
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import EvalCase, FilesystemArtifact, RunVariant

pytestmark = pytest.mark.docker


def _docker_daemon_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


# Hard gate at module level — when @pytest.mark.docker is selected the daemon
# MUST be reachable. Anything else fails loudly so CI doesn't paper over a
# missing sandbox layer.
if not _docker_daemon_ok():  # pragma: no cover — environment-dependent
    pytest.fail(
        "docker daemon is not reachable but @pytest.mark.docker tests were "
        "requested. Either start Docker or exclude `-m docker`.",
        pytrace=False,
    )


def _case(case_id: str = "c1") -> EvalCase:
    return EvalCase(id=case_id, input={"q": case_id})


def _variant(name: str = "v1") -> RunVariant:
    return RunVariant(name=name, adapter="any", config={})


def _make_fixture(root: Path) -> Path:
    fixture = root / "fixture"
    fixture.mkdir()
    (fixture / "a.txt").write_text("original line\n")
    (fixture / "subdir").mkdir()
    (fixture / "subdir" / "b.txt").write_text("keep me\n")
    return fixture


def _volume_exists(volume_name: str) -> bool:
    proc = subprocess.run(
        ["docker", "volume", "inspect", volume_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return proc.returncode == 0


def _exec_in_sidecar(volume_name: str, image: str, cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an arbitrary command inside a fresh container with the workspace
    volume mounted at /workspace. No host bind mounts. This is the same
    posture the real system-under-test container would use."""
    return subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{volume_name}:/workspace",
            image,
            *cmd,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Config / lifecycle
# ---------------------------------------------------------------------------


def test_unknown_copy_from_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="copy_from path does not exist"):
        DockerVolumeAdapter(copy_from=str(tmp_path / "nope"))


def test_factory_registers_docker_volume() -> None:
    from eval_harness.factories import workspace_factory

    assert "docker_volume" in workspace_factory.registry.names()


async def test_prepare_creates_volume_with_seeded_contents(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    adapter = DockerVolumeAdapter(copy_from=str(fixture))
    workspace = await adapter.prepare(_case(), _variant())
    try:
        volume_name = workspace.metadata["volume_name"]
        assert isinstance(volume_name, str) and volume_name.startswith("evalh-")
        assert _volume_exists(volume_name)
        # Inspect the volume's contents via a sidecar — proves the seed
        # actually landed inside the volume, not just in the host staging dir.
        result = _exec_in_sidecar(
            volume_name, "alpine:3.20", ["cat", "/workspace/a.txt"]
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout == "original line\n"
        result = _exec_in_sidecar(
            volume_name, "alpine:3.20", ["cat", "/workspace/subdir/b.txt"]
        )
        assert result.returncode == 0
        assert result.stdout == "keep me\n"
    finally:
        await adapter.cleanup(workspace)
    assert not _volume_exists(volume_name)


async def test_collect_artifacts_diffs_after_modification(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    adapter = DockerVolumeAdapter(copy_from=str(fixture))
    workspace = await adapter.prepare(_case(), _variant())
    try:
        volume_name = workspace.metadata["volume_name"]
        # System under test stand-in: modify a.txt, add a new file, delete b.txt.
        rc = _exec_in_sidecar(
            volume_name,
            "alpine:3.20",
            [
                "sh",
                "-c",
                "echo 'changed line' > /workspace/a.txt && "
                "echo 'new content' > /workspace/new.txt && "
                "rm /workspace/subdir/b.txt",
            ],
        )
        assert rc.returncode == 0, rc.stderr

        artifact = await adapter.collect_artifacts(workspace)
        assert artifact.workspace_kind == "docker_volume"
        assert artifact.diff.modified == ["a.txt"]
        assert artifact.diff.added == ["new.txt"]
        assert artifact.diff.removed == ["subdir/b.txt"]
        # text_diffs body for the modified file.
        body = artifact.diff.text_diffs["a.txt"]
        assert "-original line" in body
        assert "+changed line" in body
    finally:
        await adapter.cleanup(workspace)


async def test_cleanup_removes_volume(tmp_path: Path) -> None:
    fixture = _make_fixture(tmp_path)
    adapter = DockerVolumeAdapter(copy_from=str(fixture))
    workspace = await adapter.prepare(_case(), _variant())
    volume_name = workspace.metadata["volume_name"]
    assert _volume_exists(volume_name)
    await adapter.cleanup(workspace)
    assert not _volume_exists(volume_name)


async def test_collect_without_prepare_raises(tmp_path: Path) -> None:
    from eval_harness.adapters.workspace.base import Workspace

    adapter = DockerVolumeAdapter()
    ws = Workspace(path=tmp_path, metadata={})
    with pytest.raises(AdapterError, match="volume_name"):
        await adapter.collect_artifacts(ws)


# ---------------------------------------------------------------------------
# Contract parity vs tempdir_snapshot
# ---------------------------------------------------------------------------


async def test_filesystem_artifact_shape_matches_tempdir_snapshot(tmp_path: Path) -> None:
    """The same fixture run through both adapters produces a
    FilesystemArtifact with the same pydantic schema and the same
    added/removed/modified categorisation. text-diff bodies are
    workspace-implementation detail (difflib vs docker cp + difflib here,
    git diff in the git case) and intentionally not compared."""
    fixture = _make_fixture(tmp_path)

    async def run(adapter: TempdirSnapshotAdapter | DockerVolumeAdapter) -> FilesystemArtifact:
        ws = await adapter.prepare(_case(), _variant())
        try:
            volume_name = ws.metadata.get("volume_name")
            if isinstance(volume_name, str):
                rc = _exec_in_sidecar(
                    volume_name,
                    "alpine:3.20",
                    [
                        "sh",
                        "-c",
                        "echo 'changed' > /workspace/a.txt && "
                        "echo 'hi' > /workspace/added.txt && "
                        "rm /workspace/subdir/b.txt",
                    ],
                )
                assert rc.returncode == 0, rc.stderr
            else:
                (ws.path / "a.txt").write_text("changed\n")
                (ws.path / "added.txt").write_text("hi\n")
                (ws.path / "subdir" / "b.txt").unlink()
            return await adapter.collect_artifacts(ws)
        finally:
            await adapter.cleanup(ws)

    a_temp = await run(TempdirSnapshotAdapter(copy_from=str(fixture)))
    a_dvol = await run(DockerVolumeAdapter(copy_from=str(fixture)))

    assert type(a_temp) is type(a_dvol) is FilesystemArtifact
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
    # before-manifests agree on file set + sha256s (same fixture seed).
    assert set(a_temp.before_manifest.files) == set(a_dvol.before_manifest.files)
    for p in a_temp.before_manifest.files:
        assert (
            a_temp.before_manifest.files[p].sha256
            == a_dvol.before_manifest.files[p].sha256
        )
    # Diff categorisation agrees.
    assert sorted(a_temp.diff.added) == sorted(a_dvol.diff.added)
    assert sorted(a_temp.diff.removed) == sorted(a_dvol.diff.removed)
    assert sorted(a_temp.diff.modified) == sorted(a_dvol.diff.modified)


# ---------------------------------------------------------------------------
# *** SECURITY HEADLINE *** sandbox cannot read host $HOME
# ---------------------------------------------------------------------------


async def test_sandbox_cannot_read_host_home_ssh(tmp_path: Path) -> None:
    """The headline ev-yry sandbox invariant.

    A real system-under-test container that mounts only the workspace volume
    MUST NOT be able to read the host operator's ``$HOME/.ssh``. We prove it
    by:

    1. Creating a sentinel file under ``$HOME/.ssh`` on the host with random
       content (so a stray bind mount would be detectable).
    2. Spawning a sidecar with the *workspace volume only* mounted (the same
       posture a system adapter would use).
    3. From inside the sidecar, attempting to read the host path *and* to
       walk every host root mount point at /. Both must fail / show nothing
       sensitive.

    A regression here — e.g. someone adding ``-v $HOME:/host`` to the helper
    container — would let the sentinel leak into stdout and fail the test.
    """
    fixture = _make_fixture(tmp_path)

    home = Path(os.environ.get("HOME", "/root"))
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(exist_ok=True)
    sentinel = ssh_dir / f"evalh_sandbox_sentinel_{uuid.uuid4().hex}"
    secret = f"DO_NOT_LEAK_{uuid.uuid4().hex}"
    sentinel.write_text(secret)
    try:
        adapter = DockerVolumeAdapter(copy_from=str(fixture))
        workspace = await adapter.prepare(_case(), _variant())
        try:
            volume_name = workspace.metadata["volume_name"]

            # 1) The host-absolute path to the sentinel must not be reachable
            #    from inside the container. (Inside the container, /Users or
            #    /home doesn't exist by default — alpine has no such dirs.)
            rc = _exec_in_sidecar(
                volume_name,
                "alpine:3.20",
                ["cat", str(sentinel)],
            )
            assert rc.returncode != 0, (
                "SECURITY REGRESSION: sandbox CAN read host file "
                f"{sentinel}: stdout={rc.stdout!r}"
            )
            assert secret not in rc.stdout

            # 2) Recursively search /workspace for the secret — proves the
            #    seed pipeline didn't accidentally hoover up host files.
            rc = _exec_in_sidecar(
                volume_name,
                "alpine:3.20",
                ["grep", "-rF", "--", secret, "/workspace"],
            )
            # grep returns 1 when there are no matches; that's the good case.
            assert rc.returncode == 1, (
                f"SECURITY REGRESSION: secret was found inside /workspace: "
                f"rc={rc.returncode} stdout={rc.stdout!r}"
            )
            assert secret not in rc.stdout

            # 3) Snapshot every top-level dir inside the container; ensure
            #    /Users (macOS host home root) and /home (linux host home
            #    root) are absent unless they were created by the alpine
            #    image itself.
            rc = _exec_in_sidecar(
                volume_name, "alpine:3.20", ["ls", "-1", "/"]
            )
            assert rc.returncode == 0
            top_level = set(rc.stdout.split())
            # alpine ships /home but it is empty.
            assert "Users" not in top_level
            # If /home exists (it does on alpine), it must be empty.
            if "home" in top_level:
                rc_home = _exec_in_sidecar(
                    volume_name, "alpine:3.20", ["ls", "-1", "/home"]
                )
                assert rc_home.returncode == 0
                assert rc_home.stdout.strip() == "", (
                    f"SECURITY REGRESSION: /home is non-empty inside the "
                    f"sandbox: {rc_home.stdout!r}"
                )
        finally:
            await adapter.cleanup(workspace)
    finally:
        sentinel.unlink(missing_ok=True)
