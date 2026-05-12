"""Shared helper: ship a built FilesystemArtifact via an `ObjectStorage`.

Called from `tempdir_snapshot`, `git_workspace`, and `docker_volume`
adapters' ``collect_artifacts`` after they build the artifact. When the
caller didn't wire an ObjectStorage (the v0/v1 single-machine default),
this is a no-op.

The published key is stable: ``<case_id>/<variant_name>/artifact.json``.
``FilesystemArtifact.artifacts_path`` is rewritten to the returned URL so
downstream tooling (``evalh inspect``, distributed executors in v2) can
fetch the artifact wherever it landed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from eval_harness.core.models import FilesystemArtifact

if TYPE_CHECKING:
    from eval_harness.core.object_storage.base import ObjectStorage


async def publish_artifact(
    artifact: FilesystemArtifact,
    storage: ObjectStorage | None,
) -> FilesystemArtifact:
    """Upload `artifact.model_dump_json()` via `storage` if provided.

    Returns the same `artifact` instance (potentially mutated in place,
    since `artifacts_path` is rewritten to the storage URL on success).
    """
    if storage is None:
        return artifact
    key = f"{artifact.case_id}/{artifact.variant_name}/artifact.json"
    payload = artifact.model_dump_json(indent=2).encode("utf-8")
    url = await storage.put(key, payload)
    artifact.artifacts_path = url
    return artifact
