"""`evalh promote` tests.

CliRunner + tmp_path fixtures. The filesystem-level baseline helpers
themselves are covered in `tests/unit/test_baseline.py`; this file
covers the CLI surface (option parsing, exit codes, error messages).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from eval_harness.cli.main import cli


def _seed_run(run_dir: Path, eval_name: str = "demo") -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "config.yaml").write_text(f"eval:\n  name: {eval_name}\n")
    (run_dir / "summary.yaml").write_text(f"run_id: {run_dir.name}\n")


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def test_help_lists_promote(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["promote", "--help"])
    assert result.exit_code == 0
    assert "Promote a finished run" in result.output
    assert "--as-baseline" in result.output


def test_promote_creates_symlink(runner: CliRunner, tmp_path: Path) -> None:
    run = tmp_path / "2026-05-12T10_demo"
    _seed_run(run)
    result = runner.invoke(cli, ["promote", str(run)])
    assert result.exit_code == 0, result.output
    link = tmp_path / "baselines" / "demo"
    assert link.is_symlink()
    assert link.resolve() == run.resolve()


def test_promote_replaces_existing_baseline(
    runner: CliRunner, tmp_path: Path
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _seed_run(first)
    _seed_run(second)
    runner.invoke(cli, ["promote", str(first)])
    result = runner.invoke(cli, ["promote", str(second)])
    assert result.exit_code == 0, result.output
    link = tmp_path / "baselines" / "demo"
    assert link.resolve() == second.resolve()


def test_promote_eval_name_override(runner: CliRunner, tmp_path: Path) -> None:
    run = tmp_path / "r1"
    _seed_run(run, eval_name="from-config")
    result = runner.invoke(cli, ["promote", str(run), "--eval-name", "from-flag"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "baselines" / "from-flag").resolve() == run.resolve()


def test_promote_runs_root_override(runner: CliRunner, tmp_path: Path) -> None:
    run = tmp_path / "runs" / "r1"
    _seed_run(run)
    elsewhere = tmp_path / "elsewhere"
    result = runner.invoke(
        cli, ["promote", str(run), "--runs-root", str(elsewhere)]
    )
    assert result.exit_code == 0, result.output
    assert (elsewhere / "baselines" / "demo").resolve() == run.resolve()


def test_promote_missing_eval_name_clean_error(
    runner: CliRunner, tmp_path: Path
) -> None:
    """A run dir with no config.yaml and no --eval-name -> clear ClickException."""
    run = tmp_path / "stranded"
    run.mkdir()
    result = runner.invoke(cli, ["promote", str(run)])
    assert result.exit_code != 0
    assert "eval_name" in result.output


def test_promote_no_as_baseline_explicit_rejected(
    runner: CliRunner, tmp_path: Path
) -> None:
    """`--no-as-baseline` is forward-compat space; it has no valid meaning
    today, so the command rejects it with an explanation rather than
    silently succeeding."""
    run = tmp_path / "r1"
    _seed_run(run)
    result = runner.invoke(cli, ["promote", str(run), "--no-as-baseline"])
    assert result.exit_code != 0
    assert "as-baseline" in result.output
