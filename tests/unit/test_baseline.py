"""Baseline-marker tests.

`runs/baselines/<eval_name>` is a symlink to the run designated as the
baseline. These tests cover the filesystem operations directly — the
drift CLI (next bead) consumes the same helpers.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from eval_harness.core.baseline import (
    baseline_path,
    get_baseline_run,
    list_baselines,
    promote_run_to_baseline,
)
from eval_harness.core.errors import ConfigError


def _make_run(runs_root: Path, run_id: str, eval_name: str = "demo") -> Path:
    run_dir = runs_root / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.yaml").write_text(f"eval:\n  name: {eval_name}\n")
    (run_dir / "summary.yaml").write_text(f"run_id: {run_id}\n")
    return run_dir


# ---- baseline_path ------------------------------------------------------


def test_baseline_path_builds_under_baselines_dir(tmp_path: Path) -> None:
    assert (
        baseline_path("demo", runs_root=tmp_path)
        == tmp_path / "baselines" / "demo"
    )


def test_baseline_path_empty_eval_name_raises() -> None:
    with pytest.raises(ConfigError):
        baseline_path("", runs_root=Path("/tmp"))


# ---- promote_run_to_baseline -------------------------------------------


def test_promote_run_creates_symlink_to_run(tmp_path: Path) -> None:
    run = _make_run(tmp_path, "2026-05-12T10-00-00_demo")
    link = promote_run_to_baseline(run, runs_root=tmp_path)
    assert link.is_symlink()
    assert link.resolve() == run.resolve()
    # Symlink lives under runs/baselines/<eval_name>/
    assert link == tmp_path / "baselines" / "demo"


def test_promote_run_infers_eval_name_from_config(tmp_path: Path) -> None:
    run = _make_run(tmp_path, "r1", eval_name="custom_eval")
    link = promote_run_to_baseline(run, runs_root=tmp_path)
    assert link.name == "custom_eval"


def test_promote_run_replaces_existing_baseline(tmp_path: Path) -> None:
    """Promoting a new run swaps the symlink atomically — the old run
    is no longer the baseline."""
    first = _make_run(tmp_path, "first")
    promote_run_to_baseline(first, runs_root=tmp_path)
    second = _make_run(tmp_path, "second")
    link = promote_run_to_baseline(second, runs_root=tmp_path)
    assert link.resolve() == second.resolve()


def test_promote_run_explicit_eval_name_wins(tmp_path: Path) -> None:
    """Caller-supplied eval_name overrides config.yaml — useful when a
    run dir doesn't have a config, e.g. migrated runs."""
    run = _make_run(tmp_path, "r1", eval_name="from-config")
    link = promote_run_to_baseline(run, eval_name="from-caller", runs_root=tmp_path)
    assert link.name == "from-caller"


def test_promote_run_explicit_runs_root_wins(tmp_path: Path) -> None:
    """A separate runs_root parent — useful for repos that store baselines
    elsewhere."""
    run = _make_run(tmp_path / "runs", "r1")
    other_root = tmp_path / "elsewhere"
    link = promote_run_to_baseline(run, runs_root=other_root)
    assert link == other_root / "baselines" / "demo"


def test_promote_run_missing_dir_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="run_dir does not exist"):
        promote_run_to_baseline(tmp_path / "nope", runs_root=tmp_path)


def test_promote_run_not_a_directory_raises(tmp_path: Path) -> None:
    file = tmp_path / "not-a-dir"
    file.write_text("hi")
    with pytest.raises(ConfigError, match="not a directory"):
        promote_run_to_baseline(file, runs_root=tmp_path)


def test_promote_run_no_eval_name_resolvable_raises(tmp_path: Path) -> None:
    """Run with no config.yaml and no explicit eval_name -> ConfigError
    with a clear message."""
    run = tmp_path / "stranded"
    run.mkdir()
    with pytest.raises(ConfigError, match="could not resolve eval_name"):
        promote_run_to_baseline(run, runs_root=tmp_path)


def test_symlink_target_is_relative(tmp_path: Path) -> None:
    """Relative symlink targets survive a `runs/` move — important when
    operators copy a runs tree between machines or into version control."""
    run = _make_run(tmp_path, "r1")
    link = promote_run_to_baseline(run, runs_root=tmp_path)
    target = os.readlink(link)
    assert not os.path.isabs(target)


# ---- get_baseline_run --------------------------------------------------


def test_get_baseline_run_returns_target(tmp_path: Path) -> None:
    run = _make_run(tmp_path, "r1")
    promote_run_to_baseline(run, runs_root=tmp_path)
    resolved = get_baseline_run("demo", runs_root=tmp_path)
    assert resolved is not None
    assert resolved == run.resolve()


def test_get_baseline_run_returns_none_when_unpromoted(tmp_path: Path) -> None:
    assert get_baseline_run("never-promoted", runs_root=tmp_path) is None


def test_get_baseline_run_returns_none_for_dangling_link(tmp_path: Path) -> None:
    """An operator deleted the target run dir but left the symlink. The
    helper returns None so callers don't crash; surfacing the bug is the
    drift CLI's job."""
    run = _make_run(tmp_path, "doomed")
    promote_run_to_baseline(run, runs_root=tmp_path)
    import shutil

    shutil.rmtree(run)
    assert get_baseline_run("demo", runs_root=tmp_path) is None


# ---- list_baselines ----------------------------------------------------


def test_list_baselines_empty_when_no_baselines_dir(tmp_path: Path) -> None:
    assert list_baselines(runs_root=tmp_path) == {}


def test_list_baselines_returns_one_entry_per_promoted_eval(
    tmp_path: Path,
) -> None:
    a = _make_run(tmp_path, "ra", eval_name="alpha")
    b = _make_run(tmp_path, "rb", eval_name="bravo")
    promote_run_to_baseline(a, runs_root=tmp_path)
    promote_run_to_baseline(b, runs_root=tmp_path)
    out = list_baselines(runs_root=tmp_path)
    assert set(out) == {"alpha", "bravo"}
    assert out["alpha"] == a.resolve()
    assert out["bravo"] == b.resolve()


def test_list_baselines_skips_dangling_symlinks(tmp_path: Path) -> None:
    a = _make_run(tmp_path, "ra", eval_name="alpha")
    b = _make_run(tmp_path, "rb", eval_name="bravo")
    promote_run_to_baseline(a, runs_root=tmp_path)
    promote_run_to_baseline(b, runs_root=tmp_path)
    import shutil

    shutil.rmtree(b)
    out = list_baselines(runs_root=tmp_path)
    assert set(out) == {"alpha"}
