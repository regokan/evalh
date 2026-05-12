from __future__ import annotations

from pathlib import Path

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    ExpectedBehavior,
    FileDiff,
    FileManifest,
    FilesystemArtifact,
    Trace,
    TraceOutput,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.git_diff import GitDiffEvaluator


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


def _artifact(
    *,
    added: list[str] | None = None,
    removed: list[str] | None = None,
    modified: list[str] | None = None,
    text_diffs: dict[str, str] | None = None,
    artifacts_path: str = "/tmp/eval-artifacts",
) -> FilesystemArtifact:
    return FilesystemArtifact(
        case_id="c1",
        variant_name="v1",
        workspace_kind="tempdir_snapshot",
        before_manifest=FileManifest(files={}),
        after_manifest=FileManifest(files={}),
        diff=FileDiff(
            added=added or [],
            removed=removed or [],
            modified=modified or [],
            text_diffs=text_diffs or {},
        ),
        artifacts_path=artifacts_path,
    )


# ---- validate_config -----------------------------------------------------


def test_validate_must_modify_files_must_be_list() -> None:
    with pytest.raises(ConfigError, match="must_modify_files"):
        GitDiffEvaluator.validate_config({"must_modify_files": "a.py"})


def test_validate_must_not_modify_files_must_be_list() -> None:
    with pytest.raises(ConfigError, match="must_not_modify_files"):
        GitDiffEvaluator.validate_config({"must_not_modify_files": 42})


def test_validate_expected_patch_path_must_be_str() -> None:
    with pytest.raises(ConfigError, match="expected_patch_path"):
        GitDiffEvaluator.validate_config({"expected_patch_path": ["a.patch"]})


# ---- pass cases ----------------------------------------------------------


async def test_pass_when_required_modify_and_no_forbidden() -> None:
    ev = GitDiffEvaluator(
        name="diff",
        must_modify_files=["src/foo.py"],
        must_not_modify_files=["README.md"],
    )
    artifact = _artifact(modified=["src/foo.py"], added=["src/foo_new.py"])
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is True
    assert result.reason == "ok"
    assert "src/foo.py" not in result.detail["missing_must_modify"]


async def test_pass_uses_case_expected_when_no_config_override() -> None:
    ev = GitDiffEvaluator(name="diff")
    case = EvalCase(
        id="c1",
        input={},
        expected=ExpectedBehavior(
            must_modify_files=["app.py"], must_not_modify_files=["secrets.env"]
        ),
    )
    artifact = _artifact(modified=["app.py"])
    result = await ev.evaluate(case, _trace(), artifact)
    assert result.passed is True


# ---- fail cases ----------------------------------------------------------


async def test_fail_when_required_file_not_modified() -> None:
    ev = GitDiffEvaluator(name="diff", must_modify_files=["src/foo.py", "src/bar.py"])
    artifact = _artifact(modified=["src/foo.py"])
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is False
    assert "missing must_modify" in result.reason
    assert result.detail["missing_must_modify"] == ["src/bar.py"]


async def test_fail_when_forbidden_file_modified() -> None:
    ev = GitDiffEvaluator(name="diff", must_not_modify_files=["secrets.env", "README.md"])
    artifact = _artifact(modified=["secrets.env"], removed=["README.md"])
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is False
    assert "forbidden modified" in result.reason
    assert set(result.detail["forbidden_modified"]) == {"secrets.env", "README.md"}


async def test_fail_when_patch_does_not_match(tmp_path: Path) -> None:
    expected = tmp_path / "expected.patch"
    expected.write_text("--- a/x\n+++ b/x\n@@ @@ -1 +1 @@\n-a\n+b\n")
    ev = GitDiffEvaluator(name="diff", expected_patch_path=str(expected))
    artifact = _artifact(
        modified=["x"], text_diffs={"x": "--- a/x\n+++ b/x\n@@ @@ -1 +1 @@\n-completely\n+different\n"}
    )
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is False
    assert "patch mismatch" in result.reason
    assert result.detail["patch_check"]["matches"] is False


async def test_pass_when_patch_matches(tmp_path: Path) -> None:
    diff_text = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b"
    expected = tmp_path / "expected.patch"
    expected.write_text(diff_text)
    ev = GitDiffEvaluator(name="diff", expected_patch_path=str(expected))
    artifact = _artifact(modified=["x"], text_diffs={"x": diff_text})
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is True
    assert result.detail["patch_check"]["matches"] is True


# ---- error cases ---------------------------------------------------------


async def test_error_when_no_artifact() -> None:
    ev = GitDiffEvaluator(name="diff", must_modify_files=["x.py"])
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), None)
    assert result.passed is False
    assert result.error is not None
    assert result.error.type == "missing_artifact"


async def test_error_when_expected_patch_path_missing(tmp_path: Path) -> None:
    ev = GitDiffEvaluator(
        name="diff", expected_patch_path=str(tmp_path / "nope.patch")
    )
    artifact = _artifact(modified=["x"], text_diffs={"x": "diff"})
    result = await ev.evaluate(EvalCase(id="c1", input={}), _trace(), artifact)
    assert result.passed is False
    assert "patch mismatch" in result.reason
    assert "does not exist" in result.detail["patch_check"]["reason"]


def test_evaluator_factory_registers_git_diff() -> None:
    from eval_harness.factories import evaluator_factory

    assert "git_diff" in evaluator_factory.registry.names()
