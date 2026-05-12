"""Command evaluator — runs a subprocess against the artifact directory and
passes when the exit code matches.

`cwd` is pinned to the FilesystemArtifact's `artifacts_path` by default,
NEVER the source workspace (the system has already finished running by the
time evaluators see anything). See `.claude/rules/security.md`.

`subprocess` is invoked with `shell=False`; the config takes `cmd: list[str]`
exclusively. Strings are rejected at validate-time so callers can't slip a
shell snippet through.
"""

from __future__ import annotations

import asyncio
import contextlib
import shlex
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

_DEFAULT_TIMEOUT_SECONDS = 60
_DEFAULT_EXIT_CODE = 0
_OUTPUT_BYTES_CAP = 64 * 1024  # 64 KiB stdout/stderr in detail


class CommandEvaluator(Evaluator):
    type: ClassVar[str] = "command"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        cmd = config.get("cmd")
        if cmd is None:
            raise ConfigError("command: 'cmd' is required (list[str])")
        if isinstance(cmd, str):
            raise ConfigError(
                f"command: 'cmd' must be a list[str], not a shell string "
                f"(got {cmd!r}). Pass argv as a list — shell=True is forbidden "
                f"for security. To split a string at config-load time, use "
                f"YAML's list syntax: cmd: [{shlex.quote(cmd)}]"
            )
        if not isinstance(cmd, list) or not all(isinstance(a, str) for a in cmd) or not cmd:
            raise ConfigError("command: 'cmd' must be a non-empty list[str]")
        timeout = config.get("timeout_seconds")
        if timeout is not None and (
            not isinstance(timeout, int | float)
            or isinstance(timeout, bool)
            or timeout <= 0
        ):
            raise ConfigError("command: 'timeout_seconds' must be a positive number")
        expected = config.get("expected_exit_code")
        if expected is not None and (
            not isinstance(expected, int) or isinstance(expected, bool)
        ):
            raise ConfigError("command: 'expected_exit_code' must be an int")
        cwd = config.get("cwd")
        if cwd is not None and not isinstance(cwd, str):
            raise ConfigError("command: 'cwd' must be a string")
        if "capture_output" in config and not isinstance(config["capture_output"], bool):
            raise ConfigError("command: 'capture_output' must be bool")

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()

        cfg = self._config
        cmd = list(cfg["cmd"])
        expected_exit = int(cfg.get("expected_exit_code", _DEFAULT_EXIT_CODE))
        timeout = float(cfg.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS))
        capture = bool(cfg.get("capture_output", True))
        cwd_spec = cfg.get("cwd", "artifact")

        cwd: Path | None
        if cwd_spec == "artifact":
            if artifact is None:
                return _error_result(
                    self,
                    case,
                    trace,
                    started,
                    "missing_artifact",
                    "command: cwd='artifact' but no FilesystemArtifact available",
                )
            cwd = Path(artifact.artifacts_path)
        else:
            cwd = Path(cwd_spec)

        if not cwd.exists() or not cwd.is_dir():
            return _error_result(
                self,
                case,
                trace,
                started,
                "missing_cwd",
                f"command: cwd does not exist or is not a directory: {cwd}",
            )

        stdout_text = ""
        stderr_text = ""
        try:
            stdout_bytes, stderr_bytes, exit_code = await _run_subprocess(
                cmd=cmd, cwd=cwd, timeout=timeout, capture=capture
            )
        except TimeoutError:
            return _error_result(
                self,
                case,
                trace,
                started,
                "timeout",
                f"command: timed out after {timeout}s: {cmd!r}",
            )
        except FileNotFoundError as e:
            return _error_result(
                self,
                case,
                trace,
                started,
                "command_not_found",
                f"command: executable not found: {e}",
            )
        except OSError as e:
            return _error_result(
                self,
                case,
                trace,
                started,
                "os_error",
                f"command: OS error invoking {cmd!r}: {e}",
            )

        if capture:
            stdout_text = _decode_capped(stdout_bytes)
            stderr_text = _decode_capped(stderr_bytes)

        passed = exit_code == expected_exit
        reason = (
            "exit code matched"
            if passed
            else f"exit code {exit_code} != expected {expected_exit}"
        )

        detail: dict[str, Any] = {
            "cmd": cmd,
            "cwd": str(cwd),
            "exit_code": exit_code,
            "expected_exit_code": expected_exit,
        }
        if capture:
            detail["stdout"] = stdout_text
            detail["stderr"] = stderr_text

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


async def _run_subprocess(
    *,
    cmd: list[str],
    cwd: Path,
    timeout: float,
    capture: bool,
) -> tuple[bytes, bytes, int]:
    stdout = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
    stderr = asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(cwd),
        stdout=stdout,
        stderr=stderr,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        # Tear the process down so it doesn't outlive the evaluator.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise
    return (stdout_b or b"", stderr_b or b"", int(proc.returncode or 0))


def _decode_capped(data: bytes) -> str:
    if len(data) > _OUTPUT_BYTES_CAP:
        head = data[:_OUTPUT_BYTES_CAP].decode("utf-8", errors="replace")
        return f"{head}\n…[truncated {len(data) - _OUTPUT_BYTES_CAP} bytes]"
    return data.decode("utf-8", errors="replace")


def _error_result(
    evaluator: CommandEvaluator,
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
