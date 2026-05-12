from __future__ import annotations

import sys
from pathlib import Path

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    FileDiff,
    FileManifest,
    FilesystemArtifact,
    Trace,
    TraceOutput,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.command import CommandEvaluator


def _trace() -> Trace:
    now = utc_now()
    return Trace(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=now,
        finished_at=now,
        latency_ms=1,
        input={},
        output=TraceOutput(final_answer="ok"),
    )


def _artifact(artifacts_path: Path) -> FilesystemArtifact:
    return FilesystemArtifact(
        case_id="c1",
        variant_name="v1",
        workspace_kind="tempdir_snapshot",
        before_manifest=FileManifest(files={}),
        after_manifest=FileManifest(files={}),
        diff=FileDiff(added=[], removed=[], modified=[]),
        artifacts_path=str(artifacts_path),
    )


# ---- validate_config -----------------------------------------------------


def test_validate_requires_cmd() -> None:
    with pytest.raises(ConfigError, match="'cmd' is required"):
        CommandEvaluator.validate_config({})


def test_validate_rejects_shell_string() -> None:
    """SECURITY: a shell snippet must be rejected. Only argv lists allowed."""
    with pytest.raises(ConfigError, match="shell"):
        CommandEvaluator.validate_config({"cmd": "rm -rf /"})


def test_validate_rejects_empty_list() -> None:
    with pytest.raises(ConfigError):
        CommandEvaluator.validate_config({"cmd": []})


def test_validate_rejects_non_str_elements() -> None:
    with pytest.raises(ConfigError):
        CommandEvaluator.validate_config({"cmd": ["ls", 42]})


def test_validate_rejects_non_positive_timeout() -> None:
    with pytest.raises(ConfigError, match="timeout"):
        CommandEvaluator.validate_config({"cmd": ["ls"], "timeout_seconds": 0})


def test_validate_rejects_non_int_expected_exit() -> None:
    with pytest.raises(ConfigError, match="expected_exit_code"):
        CommandEvaluator.validate_config({"cmd": ["ls"], "expected_exit_code": "0"})


# ---- pass cases ----------------------------------------------------------


async def test_pass_when_exit_code_zero(tmp_path: Path) -> None:
    ev = CommandEvaluator(name="cmd", cmd=[sys.executable, "-c", "import sys; sys.exit(0)"])
    artifact = _artifact(tmp_path)
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is True
    assert result.detail["exit_code"] == 0
    assert result.detail["cwd"] == str(tmp_path)


async def test_pass_when_exit_code_matches_non_zero_expected(tmp_path: Path) -> None:
    ev = CommandEvaluator(
        name="cmd",
        cmd=[sys.executable, "-c", "import sys; sys.exit(7)"],
        expected_exit_code=7,
    )
    artifact = _artifact(tmp_path)
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is True
    assert result.detail["exit_code"] == 7


async def test_captures_stdout_stderr(tmp_path: Path) -> None:
    ev = CommandEvaluator(
        name="cmd",
        cmd=[
            sys.executable,
            "-c",
            "import sys; print('hi'); print('err', file=sys.stderr); sys.exit(0)",
        ],
    )
    artifact = _artifact(tmp_path)
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is True
    assert "hi" in result.detail["stdout"]
    assert "err" in result.detail["stderr"]


# ---- fail cases ----------------------------------------------------------


async def test_fail_when_exit_code_differs(tmp_path: Path) -> None:
    ev = CommandEvaluator(name="cmd", cmd=[sys.executable, "-c", "import sys; sys.exit(2)"])
    artifact = _artifact(tmp_path)
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is False
    assert "exit code 2" in result.reason
    assert result.detail["exit_code"] == 2


# ---- error cases ---------------------------------------------------------


async def test_error_when_command_not_found(tmp_path: Path) -> None:
    ev = CommandEvaluator(name="cmd", cmd=["this-binary-does-not-exist-xyz"])
    artifact = _artifact(tmp_path)
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is False
    assert result.error is not None
    assert result.error.type == "command_not_found"


async def test_error_when_cwd_missing_artifact_required() -> None:
    """Default cwd='artifact'; no artifact -> clear error, no subprocess
    invoked."""
    ev = CommandEvaluator(name="cmd", cmd=[sys.executable, "-c", "pass"])
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), None)
    assert result.passed is False
    assert result.error is not None
    assert result.error.type == "missing_artifact"


async def test_error_on_timeout(tmp_path: Path) -> None:
    ev = CommandEvaluator(
        name="cmd",
        cmd=[sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=0.5,
    )
    artifact = _artifact(tmp_path)
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is False
    assert result.error is not None
    assert result.error.type == "timeout"


# ---- security checks -----------------------------------------------------


async def test_cwd_pinned_to_artifact_not_source(tmp_path: Path) -> None:
    """Even if the case has a workspace concept, the evaluator's cwd is the
    artifact path — never the source workspace."""
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    ev = CommandEvaluator(name="cmd", cmd=[sys.executable, "-c", "import os; print(os.getcwd())"])
    artifact = _artifact(artifact_dir)
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is True
    assert str(artifact_dir.resolve()) in result.detail["stdout"] or str(
        artifact_dir
    ) in result.detail["stdout"]


async def test_explicit_cwd_used_when_set(tmp_path: Path) -> None:
    explicit = tmp_path / "elsewhere"
    explicit.mkdir()
    ev = CommandEvaluator(
        name="cmd",
        cmd=[sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=str(explicit),
    )
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    artifact = _artifact(artifact_dir)
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is True
    assert str(explicit) in result.detail["stdout"]


def test_evaluator_factory_registers_command() -> None:
    from eval_harness.factories import evaluator_factory

    assert "command" in evaluator_factory.registry.names()
