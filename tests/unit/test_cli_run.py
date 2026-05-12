from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

from click.testing import CliRunner

from eval_harness.cli.main import cli

_STUB_MODULE_NAME = "_evalh_test_cli_stub_agent"


def _install_stub() -> None:
    def agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {
            "final_answer": f"answer for {case['id']}",
            "metrics": {"token_input": 5, "token_output": 7, "cost_usd": 0.0},
        }

    mod = types.ModuleType(_STUB_MODULE_NAME)
    mod.run = agent  # type: ignore[attr-defined]
    sys.modules[_STUB_MODULE_NAME] = mod


def _write_yaml(tmp_path: Path) -> Path:
    cases = tmp_path / "cases.yaml"
    cases.write_text(
        """
schema_version: "1.0"
dataset:
  name: tiny
cases:
  - id: c1
    input: {q: 1}
  - id: c2
    input: {q: 2}
"""
    )
    eval_yaml = tmp_path / "eval.yaml"
    eval_yaml.write_text(
        f"""
eval:
  name: cli_smoke
dataset:
  type: yaml
  path: {cases.as_posix()}
systems:
  - name: stub
    adapter: python_function
    target: {_STUB_MODULE_NAME}:run
evaluators: []
run:
  max_concurrency: 2
output:
  - type: local_files
    path: {(tmp_path / 'runs').as_posix()}
"""
    )
    return eval_yaml


def test_cli_help_lists_run_command() -> None:
    runner = CliRunner()
    result = runner.invoke(cli, ["--help"])
    assert result.exit_code == 0
    assert "run" in result.output


def test_cli_run_executes_end_to_end(tmp_path: Path) -> None:
    _install_stub()
    eval_yaml = _write_yaml(tmp_path)

    runner = CliRunner()
    result = runner.invoke(cli, ["run", str(eval_yaml)])

    assert result.exit_code == 0, result.output
    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]
    assert (run_dir / "traces.jsonl").is_file()
    assert (run_dir / "summary.yaml").is_file()
    # 2 cases x 1 variant -> 2 traces.
    lines = (run_dir / "traces.jsonl").read_text().splitlines()
    assert len(lines) == 2


def test_cli_run_bad_config_yields_clean_error(tmp_path: Path) -> None:
    bad = tmp_path / "eval.yaml"
    bad.write_text("not: a valid: config:\n")

    runner = CliRunner()
    result = runner.invoke(cli, ["run", str(bad)])

    assert result.exit_code != 0
    # ClickException prints a clean "Error:" prefix and no Python traceback.
    assert "Traceback" not in result.output
    assert "Error" in result.output
