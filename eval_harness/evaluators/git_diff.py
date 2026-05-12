"""Filesystem-diff evaluator — reads `FilesystemArtifact.diff` and asserts
which files were modified vs. not.

Optionally diffs against a fixed expected unified-patch file: this is an
exact-string match after sorting per-file diff text. Patch-equivalence
("same semantic change, different line ordering") is intentionally out of
scope — write a `command` evaluator that runs `git apply --check` instead
if you need that.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
    TraceError,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.base import Evaluator


class GitDiffEvaluator(Evaluator):
    type: ClassVar[str] = "git_diff"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        for key in ("must_modify_files", "must_not_modify_files"):
            val = config.get(key)
            if val is not None and not (
                isinstance(val, list) and all(isinstance(s, str) for s in val)
            ):
                raise ConfigError(f"git_diff: '{key}' must be a list[str]")
        epp = config.get("expected_patch_path")
        if epp is not None and not isinstance(epp, str):
            raise ConfigError("git_diff: 'expected_patch_path' must be a string")

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()

        if artifact is None:
            return _error_result(
                self,
                case,
                trace,
                started,
                "missing_artifact",
                "git_diff: no FilesystemArtifact available; configure a "
                "workspace adapter that produces one",
            )

        cfg = self._config
        must_modify = list(cfg.get("must_modify_files") or case.expected.must_modify_files)
        must_not_modify = list(
            cfg.get("must_not_modify_files") or case.expected.must_not_modify_files
        )
        expected_patch_path = cfg.get("expected_patch_path")

        diff = artifact.diff
        touched_writes = set(diff.added) | set(diff.modified)
        touched_any = touched_writes | set(diff.removed)

        missing_modify = [f for f in must_modify if f not in touched_writes]
        forbidden_hits = [f for f in must_not_modify if f in touched_any]

        patch_check: dict[str, Any] | None = None
        if expected_patch_path is not None:
            patch_check = _compare_patch(expected_patch_path, diff.text_diffs)

        passed = (
            not missing_modify
            and not forbidden_hits
            and (patch_check is None or patch_check["matches"])
        )

        reason_parts: list[str] = []
        if missing_modify:
            reason_parts.append(f"missing must_modify: {missing_modify}")
        if forbidden_hits:
            reason_parts.append(f"forbidden modified: {forbidden_hits}")
        if patch_check is not None and not patch_check["matches"]:
            reason_parts.append(f"patch mismatch: {patch_check['reason']}")
        reason = "; ".join(reason_parts) if reason_parts else "ok"

        detail: dict[str, Any] = {
            "added": list(diff.added),
            "removed": list(diff.removed),
            "modified": list(diff.modified),
            "missing_must_modify": missing_modify,
            "forbidden_modified": forbidden_hits,
        }
        if patch_check is not None:
            detail["patch_check"] = patch_check

        finished = utc_now()
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=passed,
            reason=reason,
            detail=detail,
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )


def _compare_patch(
    expected_patch_path: str, text_diffs: dict[str, str]
) -> dict[str, Any]:
    path = Path(expected_patch_path)
    if not path.exists():
        return {
            "matches": False,
            "reason": f"expected_patch_path does not exist: {expected_patch_path}",
        }
    try:
        expected = path.read_text()
    except OSError as e:
        return {
            "matches": False,
            "reason": f"could not read expected_patch_path: {e}",
        }
    actual = "\n".join(text_diffs[f] for f in sorted(text_diffs))
    matches = expected.strip() == actual.strip()
    return {
        "matches": matches,
        "reason": "exact match" if matches else "diff text differs",
        "expected_path": expected_patch_path,
    }


def _error_result(
    evaluator: GitDiffEvaluator,
    case: EvalCase,
    trace: Trace,
    started: Any,
    error_type: str,
    message: str,
) -> EvaluationResult:
    finished = utc_now()
    return EvaluationResult(
        run_id=trace.run_id,
        case_id=case.id,
        variant_name=trace.variant_name,
        evaluator=evaluator.name,
        evaluator_type=evaluator.type,
        passed=False,
        reason=message,
        detail={},
        started_at=started,
        finished_at=finished,
        latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        error=TraceError(type=error_type, message=message),
    )
