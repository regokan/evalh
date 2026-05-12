from __future__ import annotations

import asyncio
import shutil
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.adapters.workspace.tempdir_snapshot_adapter import (
    _build_manifest,
    _build_manifest_with_text_cache,
    _seed_workspace,
)
from eval_harness.adapters.workspace.tempdir_snapshot_adapter import (
    _safe as _safe_name,
)
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import (
    EvalCase,
    FileDiff,
    FileManifest,
    FilesystemArtifact,
    RunVariant,
)

if TYPE_CHECKING:
    import pygit2

_AUTHOR_NAME = "eval-harness"
_AUTHOR_EMAIL = "eval-harness@invalid"
_INITIAL_COMMIT_MSG = "initial workspace snapshot"
_HEAD_REF = "HEAD"


class GitWorkspaceAdapter:
    """Workspace adapter that captures changes as a git diff.

    Same Protocol shape as `tempdir_snapshot` and the same `FilesystemArtifact`
    contract. Difference: text-diff bodies come from `git diff` (richer than
    difflib), and `workspace.metadata` carries commit SHAs / branch info.

    Requires `pygit2` (the `[git]` extra). The import is deferred so installing
    eval-harness without the extra still works for users who never touch this
    adapter.
    """

    name: str

    def __init__(
        self,
        name: str = "git",
        *,
        copy_from: str | None = None,
        base_path: str | None = None,
        init_git: bool = True,
        **_extra: Any,
    ) -> None:
        try:
            import pygit2  # noqa: F401
        except ImportError as e:
            raise ConfigError(
                "git workspace adapter requires pygit2. Install with: "
                "pip install 'eval-harness[git]'"
            ) from e

        self.name = name
        self._copy_from = Path(copy_from).expanduser().resolve() if copy_from else None
        if self._copy_from is not None and not self._copy_from.exists():
            raise ConfigError(
                f"git workspace: copy_from path does not exist: {self._copy_from}"
            )
        self._base_path = Path(base_path).expanduser() if base_path else None
        self._init_git = init_git

    async def prepare(self, case: EvalCase, variant: RunVariant) -> Workspace:
        if self._base_path is not None:
            self._base_path.mkdir(parents=True, exist_ok=True)
        prefix = f"evalh-git-{_safe_name(case.id)}-{_safe_name(variant.name)}-"
        tmp = Path(
            tempfile.mkdtemp(
                prefix=prefix,
                dir=str(self._base_path) if self._base_path else None,
            )
        )

        if self._copy_from is not None:
            await asyncio.to_thread(_seed_workspace, self._copy_from, tmp)

        meta = await asyncio.to_thread(self._init_or_open_repo, tmp)
        before, before_text_cache = await asyncio.to_thread(
            _build_manifest_with_text_cache, tmp
        )
        before = _strip_git(before)
        before_text_cache = {
            k: v for k, v in before_text_cache.items() if not _is_git_path(k)
        }

        return Workspace(
            path=tmp,
            metadata={
                "case_id": case.id,
                "variant_name": variant.name,
                "before_manifest": before.model_dump(),
                "before_text_cache": before_text_cache,
                "git_before": meta["sha"],
                "git_branch": meta["branch"],
                "git_initial": meta["initial"],
            },
        )

    async def collect_artifacts(self, workspace: Workspace) -> FilesystemArtifact:
        before_raw = workspace.metadata.get("before_manifest")
        if not isinstance(before_raw, dict):
            raise AdapterError(
                "git workspace: workspace.metadata.before_manifest missing; "
                "prepare() was not called or metadata was clobbered"
            )
        baseline_sha = workspace.metadata.get("git_before")
        if not isinstance(baseline_sha, str):
            raise AdapterError(
                "git workspace: workspace.metadata.git_before missing"
            )

        before = FileManifest.model_validate(before_raw)
        after = _strip_git(await asyncio.to_thread(_build_manifest, workspace.path))
        diff = await asyncio.to_thread(
            self._git_diff_against, workspace.path, baseline_sha
        )

        return FilesystemArtifact(
            case_id=str(workspace.metadata.get("case_id", "")),
            variant_name=str(workspace.metadata.get("variant_name", "")),
            workspace_kind="git",
            before_manifest=before,
            after_manifest=after,
            diff=diff,
            artifacts_path=str(workspace.path),
        )

    async def cleanup(self, workspace: Workspace) -> None:
        await asyncio.to_thread(shutil.rmtree, str(workspace.path), True)

    def _init_or_open_repo(self, path: Path) -> dict[str, Any]:
        import pygit2

        git_dir = path / ".git"
        if git_dir.exists():
            repo = pygit2.Repository(str(git_dir))
            initial = False
            try:
                head = repo.head
                sha = str(head.target)
                branch = head.shorthand if head.shorthand else _HEAD_REF
            except pygit2.GitError:
                sha, branch = _commit_all(repo, _INITIAL_COMMIT_MSG)
                initial = True
        else:
            if not self._init_git:
                raise ConfigError(
                    f"git workspace: {path} has no .git directory and init_git=false"
                )
            repo = pygit2.init_repository(str(path))
            sha, branch = _commit_all(repo, _INITIAL_COMMIT_MSG)
            initial = True
        return {"sha": sha, "branch": branch, "initial": initial}

    def _git_diff_against(self, path: Path, baseline_sha: str) -> FileDiff:
        import pygit2

        repo = pygit2.Repository(str(path / ".git"))
        try:
            commit = repo[baseline_sha]
        except (KeyError, ValueError) as e:
            raise AdapterError(
                f"git workspace: baseline commit {baseline_sha} not found in repo"
            ) from e
        flags = (
            pygit2.enums.DiffOption.INCLUDE_UNTRACKED
            | pygit2.enums.DiffOption.RECURSE_UNTRACKED_DIRS
            | pygit2.enums.DiffOption.SHOW_UNTRACKED_CONTENT
        )
        diff = commit.tree.diff_to_workdir(flags)
        return _diff_to_filediff(diff)


def _is_git_path(path: str) -> bool:
    return path == ".git" or path.startswith(".git/")


def _strip_git(manifest: FileManifest) -> FileManifest:
    return FileManifest(
        files={k: v for k, v in manifest.files.items() if not _is_git_path(k)}
    )


def _commit_all(repo: pygit2.Repository, message: str) -> tuple[str, str]:
    import pygit2

    repo.index.add_all()
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature(_AUTHOR_NAME, _AUTHOR_EMAIL, 0, 0)
    parents = [repo.head.target] if not repo.head_is_unborn else []
    oid = repo.create_commit("HEAD", sig, sig, message, tree, parents)
    sha = str(oid)
    try:
        branch = repo.head.shorthand
    except pygit2.GitError:
        branch = _HEAD_REF
    return sha, branch


def _diff_to_filediff(diff: pygit2.Diff) -> FileDiff:
    import pygit2

    added: list[str] = []
    removed: list[str] = []
    modified: list[str] = []
    text_diffs: dict[str, str] = {}

    for patch in diff:
        if patch is None:
            continue
        delta = patch.delta
        status = delta.status
        if status == pygit2.enums.DeltaStatus.DELETED:
            removed.append(delta.old_file.path)
        elif status in (
            pygit2.enums.DeltaStatus.ADDED,
            pygit2.enums.DeltaStatus.UNTRACKED,
            pygit2.enums.DeltaStatus.COPIED,
        ):
            added.append(delta.new_file.path)
        elif status in (
            pygit2.enums.DeltaStatus.MODIFIED,
            pygit2.enums.DeltaStatus.TYPECHANGE,
        ):
            modified.append(delta.new_file.path)
        elif status == pygit2.enums.DeltaStatus.RENAMED:
            removed.append(delta.old_file.path)
            added.append(delta.new_file.path)
        else:
            continue
        body = (patch.text or "") if patch.text is not None else ""
        if body:
            path = (
                delta.new_file.path
                if status != pygit2.enums.DeltaStatus.DELETED
                else delta.old_file.path
            )
            text_diffs[path] = body

    return FileDiff(
        added=sorted(added),
        removed=sorted(removed),
        modified=sorted(modified),
        text_diffs=text_diffs,
    )
