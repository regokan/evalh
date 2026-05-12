from __future__ import annotations

import asyncio
import sys
from typing import Any
from unittest.mock import patch

import pytest

from eval_harness.adapters.system.cli_adapter import CliSystemAdapter
from eval_harness.core.errors import AdapterError, ConfigError, RetriableError
from eval_harness.core.models import EvalCase, RunVariant


def _case(case_id: str = "c1", user_message: str = "hello") -> EvalCase:
    return EvalCase(id=case_id, input={"user_message": user_message})


def _variant(name: str = "v1") -> RunVariant:
    return RunVariant(name=name, adapter="cli", config={})


def test_cli_adapter_rejects_missing_command() -> None:
    with pytest.raises(ConfigError, match="command"):
        CliSystemAdapter(name="x")


def test_cli_adapter_rejects_non_string_argv() -> None:
    with pytest.raises(ConfigError, match="must be a string"):
        CliSystemAdapter(name="x", command=["echo", 123])  # type: ignore[list-item]


def test_cli_adapter_rejects_bad_parse_mode() -> None:
    with pytest.raises(ConfigError, match="parse_stdout_as"):
        CliSystemAdapter(name="x", command=["echo"], parse_stdout_as="xml")


async def test_cli_adapter_open_close_lifecycle() -> None:
    adapter = CliSystemAdapter(name="x", command=["echo", "hi"])
    async with adapter as live:
        assert live is adapter


async def test_cli_adapter_run_captures_stdout() -> None:
    adapter = CliSystemAdapter(name="echo", command=["echo", "hello world"])
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    assert trace.output.final_answer == "hello world"
    assert trace.error is None
    assert trace.latency_ms >= 0


async def test_cli_adapter_timeout_raises_retriable_error() -> None:
    adapter = CliSystemAdapter(
        name="slow",
        command=[sys.executable, "-c", "import time; time.sleep(5)"],
        timeout_seconds=1,
    )
    async with adapter:
        with pytest.raises(RetriableError, match="timed out"):
            await adapter.run(_case(), _variant(), None)


async def test_cli_adapter_shell_false_no_injection() -> None:
    """A would-be shell metachar must show up verbatim in stdout, not interpret.

    If shell=True were used, ``echo $(uname)`` would substitute the kernel name.
    With shell=False the argv elements are passed literally.
    """
    adapter = CliSystemAdapter(name="echo", command=["echo", "$(uname)"])
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    assert trace.output.final_answer == "$(uname)"


async def test_cli_adapter_passes_argv_as_list_not_shell() -> None:
    """Verify we call asyncio.create_subprocess_exec with positional argv."""
    captured: dict[str, Any] = {}

    real_exec = asyncio.create_subprocess_exec

    async def _spy(*args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return await real_exec(*args, **kwargs)

    adapter = CliSystemAdapter(name="echo", command=["echo", "ok"])
    with patch("asyncio.create_subprocess_exec", _spy):
        async with adapter:
            await adapter.run(_case(), _variant(), None)

    # Each command element is a separate positional arg — never joined.
    assert captured["args"] == ("echo", "ok")
    # shell=True is never threaded through; the exec API doesn't accept it.
    assert "shell" not in captured["kwargs"]


async def test_cli_adapter_json_parse_mode_extracts_structured() -> None:
    adapter = CliSystemAdapter(
        name="echo_json",
        command=[
            sys.executable,
            "-c",
            'import json; print(json.dumps({"final_answer": "yes", "score": 3}))',
        ],
        parse_stdout_as="json",
    )
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    assert trace.output.final_answer == "yes"
    assert trace.output.structured == {"final_answer": "yes", "score": 3}


async def test_cli_adapter_nonzero_exit_raises_adapter_error() -> None:
    adapter = CliSystemAdapter(
        name="fail",
        command=[sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(2)"],
    )
    async with adapter:
        with pytest.raises(AdapterError, match=r"exited 2"):
            await adapter.run(_case(), _variant(), None)


async def test_cli_adapter_stdin_template_substitutes_case() -> None:
    adapter = CliSystemAdapter(
        name="cat",
        command=[sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        stdin_template="msg={{ input.user_message }}",
    )
    async with adapter:
        trace = await adapter.run(_case(user_message="hi there"), _variant(), None)
    assert trace.output.final_answer == "msg=hi there"


async def test_cli_adapter_env_isolated_by_default() -> None:
    """Host env is NOT inherited unless the user opts in via config.env."""
    adapter = CliSystemAdapter(
        name="env",
        command=[sys.executable, "-c", "import os; print(os.environ.get('FOO_SHOULD_NOT_LEAK','MISSING'))"],
        env={},
    )
    # Pollute host env so the test would fail if the adapter inherited it.
    import os

    os.environ["FOO_SHOULD_NOT_LEAK"] = "leaked"
    try:
        async with adapter:
            trace = await adapter.run(_case(), _variant(), None)
    finally:
        del os.environ["FOO_SHOULD_NOT_LEAK"]
    assert trace.output.final_answer == "MISSING"


async def test_cli_adapter_unknown_command_raises_adapter_error() -> None:
    adapter = CliSystemAdapter(name="ghost", command=["/no/such/binary_xyz"])
    async with adapter:
        with pytest.raises(AdapterError, match="command not found"):
            await adapter.run(_case(), _variant(), None)
