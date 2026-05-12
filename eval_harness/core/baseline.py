"""Baseline-run marker helpers.

Convention: ``<runs_root>/baselines/<eval_name>/`` is a SYMLINK to the
run directory currently designated as the baseline for that eval. The
drift CLI (``evalh promote``, ``evalh drift``) consumes this; this
module just owns the filesystem operations so the CLI and webhook sink
share one source of truth.

Why a symlink: ``ls runs/baselines/`` shows what's promoted at a
glance, no need to scan ``summary.yaml`` files; ``evalh promote`` is a
single atomic rename of the symlink. The downside (Windows symlinks
need privileges) is acceptable — v0 already requires Unix-like
semantics for ``tempdir_snapshot``.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from eval_harness.core.errors import ConfigError

_BASELINES_DIRNAME = "baselines"


def baseline_path(eval_name: str, *, runs_root: Path) -> Path:
    """`<runs_root>/baselines/<eval_name>` — the symlink path itself
    (not the resolved target)."""
    if not eval_name:
        raise ConfigError("baseline: eval_name is required")
    return runs_root / _BASELINES_DIRNAME / eval_name


def promote_run_to_baseline(
    run_dir: Path,
    *,
    eval_name: str | None = None,
    runs_root: Path | None = None,
) -> Path:
    """Designate `run_dir` as the baseline for its eval.

    Resolves ``eval_name`` from the run's ``config.yaml`` if not given,
    and infers ``runs_root`` as the run's parent if not given. Creates
    or atomically replaces the symlink. Returns the symlink path.

    Raises ``ConfigError`` if ``run_dir`` doesn't exist or
    ``eval_name`` can't be resolved.
    """
    if not run_dir.exists():
        raise ConfigError(f"baseline: run_dir does not exist: {run_dir}")
    if not run_dir.is_dir():
        raise ConfigError(f"baseline: run_dir is not a directory: {run_dir}")
    name = eval_name or _read_eval_name(run_dir)
    if not name:
        raise ConfigError(
            f"baseline: could not resolve eval_name for {run_dir}; "
            "supply eval_name= explicitly or include eval.name in "
            "config.yaml"
        )
    root = runs_root or run_dir.parent
    link = baseline_path(name, runs_root=root)
    link.parent.mkdir(parents=True, exist_ok=True)

    # Atomic replacement: write the new symlink to a sibling temp path,
    # then rename over the live link. `os.symlink` itself isn't atomic
    # against an existing path, so we go through `os.replace`.
    tmp = link.parent / f".{link.name}.tmp"
    if tmp.exists() or tmp.is_symlink():
        tmp.unlink()
    target = os.path.relpath(run_dir, link.parent)
    os.symlink(target, tmp)
    os.replace(tmp, link)
    return link


def get_baseline_run(
    eval_name: str,
    *,
    runs_root: Path,
) -> Path | None:
    """Resolve the baseline symlink for `eval_name`. Returns the
    absolute path of the target run, or ``None`` when no baseline has
    been promoted (or the symlink points at a missing path)."""
    link = baseline_path(eval_name, runs_root=runs_root)
    if not link.is_symlink() and not link.exists():
        return None
    try:
        resolved = link.resolve(strict=True)
    except (FileNotFoundError, OSError):
        return None
    return resolved


def list_baselines(*, runs_root: Path) -> dict[str, Path]:
    """`{eval_name: resolved_run_path}` for every healthy baseline link.
    Skip dangling symlinks silently — those are operator bugs the
    drift CLI surfaces explicitly when needed."""
    out: dict[str, Path] = {}
    baselines_dir = runs_root / _BASELINES_DIRNAME
    if not baselines_dir.exists():
        return out
    for entry in sorted(baselines_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        try:
            resolved = entry.resolve(strict=True)
        except (FileNotFoundError, OSError):
            continue
        out[entry.name] = resolved
    return out


def _read_eval_name(run_dir: Path) -> str | None:
    """Pull `eval.name` out of `<run_dir>/config.yaml`. Tolerates a
    missing file (returns None) — the caller surfaces the error with
    context."""
    config_path = run_dir / "config.yaml"
    if not config_path.exists():
        return None
    try:
        loaded = yaml.safe_load(config_path.read_text()) or {}
    except yaml.YAMLError:
        return None
    if not isinstance(loaded, dict):
        return None
    eval_block = loaded.get("eval")
    if not isinstance(eval_block, dict):
        return None
    name = eval_block.get("name")
    return str(name) if isinstance(name, str) and name else None


__all__ = [
    "baseline_path",
    "get_baseline_run",
    "list_baselines",
    "promote_run_to_baseline",
]
