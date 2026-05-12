"""CLI subprocess SystemAdapter — runs an argv against each case."""

from __future__ import annotations

import asyncio
import json
import re
from types import TracebackType
from typing import Any, Self

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.errors import AdapterError, ConfigError, RetriableError
from eval_harness.core.models import (
    EvalCase,
    RunVariant,
    Trace,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.core.time import utc_now

_TEMPLATE_RE = re.compile(r"\{\{\s*([^{}|]+?)(?:\s*\|\s*(json))?\s*\}\}")
_PARSE_MODES = frozenset({"text", "json"})


class CliSystemAdapter:
    """Runs a CLI agent as a subprocess via ``asyncio.create_subprocess_exec``.

    Always ``shell=False``: argv is passed as a list, never joined into a shell
    line. Host environment is **not** inherited by default — the subprocess gets
    only what ``env`` declares. Bounded by ``timeout_seconds``; timeout maps to
    ``RetriableError`` so the runner can retry per policy.
    """

    def __init__(
        self,
        name: str = "cli",
        *,
        command: list[str] | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_seconds: int = 120,
        stdin_template: str | None = None,
        parse_stdout_as: str = "text",
        metadata: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        if not command or not isinstance(command, list):
            raise ConfigError("cli adapter requires 'command' as a non-empty list[str]")
        if not all(isinstance(part, str) for part in command):
            raise ConfigError("cli adapter: every element of 'command' must be a string")
        if parse_stdout_as not in _PARSE_MODES:
            raise ConfigError(
                f"cli adapter: 'parse_stdout_as' must be one of {sorted(_PARSE_MODES)}, "
                f"got {parse_stdout_as!r}"
            )
        self.name = name
        self._command = list(command)
        self._cwd = cwd
        # Security: never inherit host env wholesale. ``env=None`` to
        # create_subprocess_exec would inherit from the parent; we always pass
        # an explicit mapping (empty by default).
        self._env: dict[str, str] = dict(env) if env is not None else {}
        self._timeout = float(timeout_seconds)
        self._stdin_template = stdin_template
        self._parse_stdout_as = parse_stdout_as

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        stdin_payload: bytes | None = None
        if self._stdin_template is not None:
            context = {
                "input": case.input,
                "case_id": case.id,
                "case": {
                    "id": case.id,
                    "input": case.input,
                    "metadata": case.metadata,
                },
            }
            stdin_payload = _render_template(self._stdin_template, context).encode("utf-8")

        started_at = utc_now()
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._command,
                cwd=self._cwd,
                env=self._env,
                stdin=asyncio.subprocess.PIPE if stdin_payload is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as e:
            raise AdapterError(
                f"cli adapter '{self.name}': command not found: {self._command[0]!r}: {e}"
            ) from e

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_payload), timeout=self._timeout
            )
        except TimeoutError as e:
            proc.kill()
            await proc.wait()
            raise RetriableError(
                f"cli adapter '{self.name}': timed out after {self._timeout}s"
            ) from e
        finished_at = utc_now()

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")

        if proc.returncode != 0:
            raise AdapterError(
                f"cli adapter '{self.name}': exited {proc.returncode}; "
                f"stderr={stderr.strip()!r}"
            )

        output = _parse_stdout(stdout, self._parse_stdout_as, self.name)

        latency_ms = max(int((finished_at - started_at).total_seconds() * 1000), 0)
        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
            input=dict(case.input),
            output=output,
            metrics=TraceMetrics(),
            extra={"stderr": stderr} if stderr else {},
        )


def _parse_stdout(stdout: str, mode: str, adapter_name: str) -> TraceOutput:
    if mode == "text":
        return TraceOutput(final_answer=stdout.rstrip("\n"))
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError as e:
        raise AdapterError(
            f"cli adapter '{adapter_name}': parse_stdout_as=json but stdout was not "
            f"valid JSON: {e}"
        ) from e
    if isinstance(parsed, dict):
        final_answer = parsed.get("final_answer")
        thinking = parsed.get("thinking")
        return TraceOutput(
            final_answer=final_answer if isinstance(final_answer, str) else None,
            thinking=thinking if isinstance(thinking, str) else None,
            structured=parsed,
        )
    return TraceOutput(structured={"value": parsed})


def _render_template(template: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        as_json = match.group(2) == "json"
        value = _resolve_path(expr, context)
        if as_json:
            return json.dumps(value)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value)

    return _TEMPLATE_RE.sub(replace, template)


def _resolve_path(expr: str, context: dict[str, Any]) -> Any:
    parts = expr.split(".")
    val: Any = context
    for part in parts:
        val = val.get(part) if isinstance(val, dict) else getattr(val, part, None)
        if val is None:
            return None
    return val
