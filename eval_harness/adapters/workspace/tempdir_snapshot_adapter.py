from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import (
    EvalCase,
    FileDiff,
    FileEntry,
    FileManifest,
    FilesystemArtifact,
    RunVariant,
)

_CHUNK_SIZE = 64 * 1024
_MAX_TEXT_DIFF_BYTES = 256 * 1024
_BINARY_PROBE_BYTES = 4096


class TempdirSnapshotAdapter:
    name: str

    def __init__(
        self,
        name: str = "tempdir_snapshot",
        *,
        copy_from: str | None = None,
        base_path: str | None = None,
        **_extra: Any,
    ) -> None:
        self.name = name
        self._copy_from = Path(copy_from).expanduser().resolve() if copy_from else None
        if self._copy_from is not None and not self._copy_from.exists():
            raise ConfigError(
                f"tempdir_snapshot: copy_from path does not exist: {self._copy_from}"
            )
        self._base_path = Path(base_path).expanduser() if base_path else None

    async def prepare(self, case: EvalCase, variant: RunVariant) -> Workspace:
        if self._base_path is not None:
            self._base_path.mkdir(parents=True, exist_ok=True)
        prefix = f"evalh-{_safe(case.id)}-{_safe(variant.name)}-"
        tmp = Path(
            tempfile.mkdtemp(
                prefix=prefix,
                dir=str(self._base_path) if self._base_path else None,
            )
        )

        if self._copy_from is not None:
            await asyncio.to_thread(_seed_workspace, self._copy_from, tmp)

        before, before_text_cache = await asyncio.to_thread(
            _build_manifest_with_text_cache, tmp
        )
        return Workspace(
            path=tmp,
            metadata={
                "case_id": case.id,
                "variant_name": variant.name,
                "before_manifest": before.model_dump(),
                "before_text_cache": before_text_cache,
            },
        )

    async def collect_artifacts(self, workspace: Workspace) -> FilesystemArtifact:
        before_raw = workspace.metadata.get("before_manifest")
        if not isinstance(before_raw, dict):
            raise AdapterError(
                "tempdir_snapshot: workspace.metadata.before_manifest missing; "
                "prepare() was not called or metadata was clobbered"
            )
        before = FileManifest.model_validate(before_raw)
        before_text_cache_raw = workspace.metadata.get("before_text_cache") or {}
        before_text_cache: dict[str, str] = (
            dict(before_text_cache_raw) if isinstance(before_text_cache_raw, dict) else {}
        )
        after = await asyncio.to_thread(_build_manifest, workspace.path)
        diff = _diff_manifests(before, after)
        diff.text_diffs = await asyncio.to_thread(
            _compute_text_diffs,
            workspace.path,
            before_text_cache,
            after,
            diff,
        )
        return FilesystemArtifact(
            case_id=str(workspace.metadata.get("case_id", "")),
            variant_name=str(workspace.metadata.get("variant_name", "")),
            workspace_kind="tempdir_snapshot",
            before_manifest=before,
            after_manifest=after,
            diff=diff,
            artifacts_path=str(workspace.path),
        )

    async def cleanup(self, workspace: Workspace) -> None:
        await asyncio.to_thread(shutil.rmtree, str(workspace.path), True)


def _safe(value: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in value)[:64]


def _seed_workspace(src: Path, dst: Path) -> None:
    for child in src.iterdir():
        target = dst / child.name
        if child.is_symlink():
            continue
        if child.is_dir():
            shutil.copytree(child, target, symlinks=False)
        else:
            shutil.copy2(child, target)


def _iter_files(root: Path) -> list[tuple[Path, str, os.stat_result]]:
    """Walk root, yielding (full_path, relative_posix_path, stat) for each regular file.

    Skips symlinks (files and directories) entirely; refuses paths that resolve
    outside root.
    """
    entries: list[tuple[Path, str, os.stat_result]] = []
    root_resolved = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [d for d in dirnames if not Path(dirpath, d).is_symlink()]
        for filename in filenames:
            full = Path(dirpath) / filename
            if full.is_symlink():
                continue
            try:
                if not full.resolve().is_relative_to(root_resolved):
                    continue
            except OSError:
                continue
            try:
                st = full.stat()
            except OSError:
                continue
            rel = str(full.relative_to(root)).replace(os.sep, "/")
            entries.append((full, rel, st))
    return entries


def _build_manifest(root: Path) -> FileManifest:
    files: dict[str, FileEntry] = {}
    for full, rel, st in _iter_files(root):
        files[rel] = FileEntry(
            size=st.st_size,
            mode=st.st_mode,
            mtime=st.st_mtime,
            sha256=_sha256(full),
        )
    return FileManifest(files=files)


def _build_manifest_with_text_cache(root: Path) -> tuple[FileManifest, dict[str, str]]:
    files: dict[str, FileEntry] = {}
    text_cache: dict[str, str] = {}
    for full, rel, st in _iter_files(root):
        entry = FileEntry(
            size=st.st_size,
            mode=st.st_mode,
            mtime=st.st_mtime,
            sha256=_sha256(full),
        )
        files[rel] = entry
        if _is_small_text_file(full, entry):
            text = _safe_read_text(full)
            if text is not None:
                text_cache[rel] = text
    return FileManifest(files=files), text_cache


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_SIZE):
            h.update(chunk)
    return h.hexdigest()


def _diff_manifests(before: FileManifest, after: FileManifest) -> FileDiff:
    before_paths = set(before.files)
    after_paths = set(after.files)
    added = sorted(after_paths - before_paths)
    removed = sorted(before_paths - after_paths)
    modified = sorted(
        p for p in before_paths & after_paths
        if before.files[p].sha256 != after.files[p].sha256
    )
    return FileDiff(added=added, removed=removed, modified=modified)


def _compute_text_diffs(
    root: Path,
    before_text_cache: dict[str, str],
    after: FileManifest,
    diff: FileDiff,
) -> dict[str, str]:
    """Unified text diffs for small text files in `modified` and `added`."""
    out: dict[str, str] = {}
    for path in diff.modified:
        full = root / path
        if not _is_small_text_file(full, after.files.get(path)):
            continue
        after_text = _safe_read_text(full)
        if after_text is None:
            continue
        before_text = before_text_cache.get(path, "")
        out[path] = _unified_diff(before_text, after_text, path)
    for path in diff.added:
        full = root / path
        if not _is_small_text_file(full, after.files.get(path)):
            continue
        after_text = _safe_read_text(full)
        if after_text is None:
            continue
        out[path] = _unified_diff("", after_text, path)
    return out


def _is_small_text_file(path: Path, entry: FileEntry | None) -> bool:
    if entry is not None and entry.size > _MAX_TEXT_DIFF_BYTES:
        return False
    try:
        with path.open("rb") as f:
            probe = f.read(_BINARY_PROBE_BYTES)
    except OSError:
        return False
    return b"\x00" not in probe


def _safe_read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _unified_diff(before_text: str, after_text: str, path: str) -> str:
    return "".join(
        difflib.unified_diff(
            before_text.splitlines(keepends=True),
            after_text.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
