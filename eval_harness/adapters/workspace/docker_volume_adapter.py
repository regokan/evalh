"""DockerVolumeAdapter — sandboxed workspace.

The system under test runs INSIDE a container; the workspace lives on a
named Docker volume mounted at ``/workspace``. This is the v1
"true filesystem isolation" path — the default ``tempdir_snapshot`` is
emphatically NOT a sandbox.

Orchestration boundary (intentional):
- WorkspaceAdapter (this file) owns: the volume, seeding from copy_from,
  snapshotting before/after, computing the diff, teardown.
- SystemAdapter owns: actually running the system in a container that
  mounts ``volume_name`` at ``/workspace``. Workspace.metadata carries
  ``volume_name`` so the system adapter can wire it.

The diff produced is the *same* FilesystemArtifact contract that
tempdir_snapshot and git_workspace produce — evaluators don't care which
workspace was used. Tests pin that contract.

This module uses the docker CLI via subprocess rather than the Python
docker SDK so the [docker] extra stays lean. ``shell=False`` everywhere
per ``.claude/rules/security.md``.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from eval_harness.adapters.workspace._artifact_publish import publish_artifact
from eval_harness.adapters.workspace.base import Workspace
from eval_harness.adapters.workspace.tempdir_snapshot_adapter import (
    _build_manifest,
    _build_manifest_with_text_cache,
    _compute_text_diffs,
    _diff_manifests,
    _safe,
    _seed_workspace,
)
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import (
    EvalCase,
    FileManifest,
    FilesystemArtifact,
    RunVariant,
)
from eval_harness.core.object_storage.base import ObjectStorage

_DEFAULT_HELPER_IMAGE = "alpine:3.20"
_DOCKER_CMD_TIMEOUT = 60.0


class DockerVolumeAdapter:
    name: str

    def __init__(
        self,
        name: str = "docker_volume",
        *,
        copy_from: str | None = None,
        image: str | None = None,
        volume_name: str | None = None,
        object_storage: ObjectStorage | None = None,
        **_extra: Any,
    ) -> None:
        if not _docker_available():
            raise ConfigError(
                "docker_volume adapter requires a working `docker` daemon. "
                "Install Docker Desktop / docker-engine and ensure `docker` is "
                "on PATH."
            )
        self.name = name
        self._copy_from = (
            Path(copy_from).expanduser().resolve() if copy_from else None
        )
        if self._copy_from is not None and not self._copy_from.exists():
            raise ConfigError(
                f"docker_volume: copy_from path does not exist: "
                f"{self._copy_from}"
            )
        self._image = image or _DEFAULT_HELPER_IMAGE
        self._volume_name_override = volume_name
        self._object_storage = object_storage

    async def prepare(self, case: EvalCase, variant: RunVariant) -> Workspace:
        volume_name = self._volume_name_override or _generate_volume_name(case, variant)
        await asyncio.to_thread(_docker, ["volume", "create", volume_name])

        # Seed the volume by copying the fixture into a host-side staging dir
        # and then `docker cp` it into a helper container that has the volume
        # mounted. We also keep the staging dir around as the canonical
        # "before" snapshot — walking it on the host is far cheaper than
        # round-tripping through docker, and the seed bytes are identical.
        host_staging = Path(tempfile.mkdtemp(prefix="evalh-dockervol-before-"))
        if self._copy_from is not None:
            await asyncio.to_thread(_seed_workspace, self._copy_from, host_staging)
            await asyncio.to_thread(
                _populate_volume_from_host, volume_name, host_staging, self._image
            )

        before, before_text_cache = await asyncio.to_thread(
            _build_manifest_with_text_cache, host_staging
        )

        return Workspace(
            path=host_staging,
            metadata={
                "case_id": case.id,
                "variant_name": variant.name,
                "before_manifest": before.model_dump(),
                "before_text_cache": before_text_cache,
                "volume_name": volume_name,
                "helper_image": self._image,
            },
        )

    async def collect_artifacts(self, workspace: Workspace) -> FilesystemArtifact:
        volume_name = workspace.metadata.get("volume_name")
        if not isinstance(volume_name, str):
            raise AdapterError(
                "docker_volume: workspace.metadata.volume_name missing; "
                "prepare() was not called or metadata was clobbered"
            )
        before_raw = workspace.metadata.get("before_manifest")
        if not isinstance(before_raw, dict):
            raise AdapterError(
                "docker_volume: workspace.metadata.before_manifest missing"
            )
        before = FileManifest.model_validate(before_raw)

        before_text_cache_raw = workspace.metadata.get("before_text_cache") or {}
        before_text_cache: dict[str, str] = (
            dict(before_text_cache_raw)
            if isinstance(before_text_cache_raw, dict)
            else {}
        )

        # Pull the post-run volume contents out to a fresh host dir so we can
        # walk + hash + diff with the standard tempdir_snapshot helpers.
        after_dir = Path(tempfile.mkdtemp(prefix="evalh-dockervol-after-"))
        try:
            await asyncio.to_thread(
                _extract_volume_to_host,
                volume_name,
                after_dir,
                str(workspace.metadata.get("helper_image") or self._image),
            )
            after = await asyncio.to_thread(_build_manifest, after_dir)
            diff = _diff_manifests(before, after)
            diff.text_diffs = await asyncio.to_thread(
                _compute_text_diffs, after_dir, before_text_cache, after, diff
            )
            artifact = FilesystemArtifact(
                case_id=str(workspace.metadata.get("case_id", "")),
                variant_name=str(workspace.metadata.get("variant_name", "")),
                workspace_kind="docker_volume",
                before_manifest=before,
                after_manifest=after,
                diff=diff,
                artifacts_path=str(after_dir),
            )
            return await publish_artifact(artifact, self._object_storage)
        except Exception:
            shutil.rmtree(after_dir, ignore_errors=True)
            raise

    async def cleanup(self, workspace: Workspace) -> None:
        volume_name = workspace.metadata.get("volume_name")
        if isinstance(volume_name, str):
            # Best-effort: a remaining container could still hold the volume;
            # `docker volume rm -f` errors are surfaced as warnings only — the
            # host staging dir cleanup must still happen.
            await asyncio.to_thread(
                _docker_safe, ["volume", "rm", "-f", volume_name]
            )
        await asyncio.to_thread(shutil.rmtree, str(workspace.path), True)


# ---------------------------------------------------------------------------
# Docker plumbing
# ---------------------------------------------------------------------------


def _docker_available() -> bool:
    """`docker info` is the canonical "daemon reachable" probe."""
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _docker(args: list[str], stdin: bytes | None = None) -> bytes:
    """Run `docker <args>`; raise AdapterError on non-zero exit."""
    try:
        proc = subprocess.run(
            ["docker", *args],
            input=stdin,
            capture_output=True,
            check=False,
            timeout=_DOCKER_CMD_TIMEOUT,
        )
    except FileNotFoundError as e:
        raise AdapterError("docker_volume: `docker` not on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise AdapterError(
            f"docker_volume: `docker {' '.join(args[:2])}` timed out after "
            f"{_DOCKER_CMD_TIMEOUT}s"
        ) from e
    if proc.returncode != 0:
        raise AdapterError(
            f"docker_volume: `docker {' '.join(args)}` failed (rc="
            f"{proc.returncode}): {proc.stderr.decode(errors='replace').strip()}"
        )
    return proc.stdout


def _docker_safe(args: list[str]) -> None:
    """Like _docker but swallows errors. Used in cleanup paths."""
    with contextlib.suppress(AdapterError):
        _docker(args)


def _generate_volume_name(case: EvalCase, variant: RunVariant) -> str:
    # Docker volume names must match [a-zA-Z0-9][a-zA-Z0-9_.-]*. The runner-
    # facing `case.id`/`variant.name` may include characters that violate that,
    # so we sanitise + append a uuid suffix for uniqueness.
    return (
        f"evalh-{_safe(case.id)}-{_safe(variant.name)}-{uuid.uuid4().hex[:12]}"
    )


def _populate_volume_from_host(
    volume_name: str, host_src: Path, image: str
) -> None:
    """Spawn a paused helper container, `docker cp` files in, remove helper.

    `docker cp` requires a target container, not a bare volume; we use a
    no-op `tail -f /dev/null` container with the volume mounted as the
    intermediary. The helper image is small (alpine) and stays only long
    enough to receive the cp.
    """
    container_id = _start_helper(volume_name, image)
    try:
        # `docker cp src/. container:/workspace` copies CONTENTS (the trailing
        # /. ), not the directory itself.
        _docker(["cp", f"{host_src!s}/.", f"{container_id}:/workspace"])
    finally:
        _docker_safe(["rm", "-f", container_id])


def _extract_volume_to_host(volume_name: str, host_dst: Path, image: str) -> None:
    container_id = _start_helper(volume_name, image)
    try:
        # Mirror image of the populate step.
        _docker(["cp", f"{container_id}:/workspace/.", str(host_dst)])
    finally:
        _docker_safe(["rm", "-f", container_id])


def _start_helper(volume_name: str, image: str) -> str:
    """Start a long-lived no-op container with the volume at /workspace.

    Returns the container ID. Caller is responsible for `docker rm -f`.
    Note the security posture: only the volume is mounted. No host paths,
    no /var/run/docker.sock, no -v $HOME -- the container's view of the
    host filesystem is exactly /workspace and the image's default layers.
    """
    out = _docker(
        [
            "run",
            "-d",
            "--rm",
            "-v",
            f"{volume_name}:/workspace",
            image,
            "tail",
            "-f",
            "/dev/null",
        ]
    )
    return out.decode().strip()


def _user_visible_home() -> str:  # pragma: no cover — convenience for tests
    return os.environ.get("HOME", "/root")
