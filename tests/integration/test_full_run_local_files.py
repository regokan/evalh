from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import yaml  # type: ignore[import-untyped]

from eval_harness.core.config_loader import load_config
from eval_harness.core.models import RunSummary, Trace
from eval_harness.runner import build_plan, run_eval

_TINY_DEMO = Path(__file__).resolve().parents[2] / "examples" / "tiny_demo" / "eval.yaml"


class _FakeUsage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAnthropicResponse:
    def __init__(self, content: list[Any], stop_reason: str = "end_turn") -> None:
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _FakeUsage(50, 25)


async def _fake_messages_create(**_: Any) -> _FakeAnthropicResponse:
    return _FakeAnthropicResponse(
        content=[
            _FakeTextBlock(
                "Richmond has an average suburb price of $1.2M; the listing "
                "is close to that average."
            )
        ]
    )


class _FakeMessages:
    def __init__(self) -> None:
        self.create = AsyncMock(side_effect=_fake_messages_create)


class _FakeAsyncAnthropic:
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.messages = _FakeMessages()


async def test_full_run_writes_all_four_files(
    tmp_path: Path,
    monkeypatch: Any,
    fake_judge_backend: None,
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

    config = load_config(_TINY_DEMO)
    config.output[0].path = str(tmp_path / "runs")
    config.dataset.path = str(_TINY_DEMO.parent / "cases.yaml")

    # Preload the agent module so we can swap its AsyncAnthropic symbol.
    agent_mod = importlib.import_module("examples.tiny_demo.agent")

    with patch.object(agent_mod, "AsyncAnthropic", _FakeAsyncAnthropic):
        plan = await build_plan(config, _TINY_DEMO)
        summary = await run_eval(plan)

    assert isinstance(summary, RunSummary)
    run_dirs = list((tmp_path / "runs").iterdir())
    assert len(run_dirs) == 1
    run_dir = run_dirs[0]

    for fname in ("config.yaml", "traces.jsonl", "results.jsonl", "summary.yaml"):
        assert (run_dir / fname).is_file(), f"missing {fname}"

    yaml.safe_load((run_dir / "config.yaml").read_text())

    trace_lines = (run_dir / "traces.jsonl").read_text().splitlines()
    assert len(trace_lines) == 6
    for line in trace_lines:
        Trace.model_validate(json.loads(line))

    summary_yaml = yaml.safe_load((run_dir / "summary.yaml").read_text())
    rebuilt = RunSummary.model_validate(summary_yaml)
    assert len(rebuilt.variants) == 2
    assert rebuilt.comparison is not None
    assert rebuilt.comparison.baseline == "agent_concise"
    assert any(d.variant == "agent_verbose" for d in rebuilt.comparison.deltas)
