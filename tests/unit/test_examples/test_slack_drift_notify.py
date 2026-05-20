"""Tests for examples/slack_drift_notify.

The example must:
  1. Load its eval.yaml cleanly when SLACK_WEBHOOK_URL is unset
     (env-var default expands to "" — that's the offline contract).
  2. Build a runnable plan whose output sinks contain BOTH local_files
     and a webhook sink with an empty url (which will short-circuit).

We don't actually run the eval through the runner here — that's covered
indirectly by regression_gate's own coverage and by the
test_empty_url_disables_sink_without_error unit test in
test_webhook_trace_store.py. This file is the example-scoped contract
test the bead's retrospective lesson asks for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_harness.core.config_loader import load_config

_EVAL_PATH = (
    Path(__file__).resolve().parents[3]
    / "examples"
    / "slack_drift_notify"
    / "eval.yaml"
)


def test_eval_yaml_loads_when_slack_webhook_url_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    config = load_config(_EVAL_PATH)
    assert config.eval.name == "slack_drift_notify"

    sinks = [s.model_dump() for s in config.output]
    assert len(sinks) == 2, "example must ship both local_files and webhook"
    sink_types = [s["type"] for s in sinks]
    assert "local_files" in sink_types
    assert "webhook" in sink_types

    webhook = next(s for s in sinks if s["type"] == "webhook")
    assert webhook["platform"] == "slack"
    assert webhook["url"] == "", (
        "url must expand to empty string when SLACK_WEBHOOK_URL is unset — "
        "that's what the webhook store treats as 'disabled'"
    )


def test_eval_yaml_loads_when_slack_webhook_url_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "SLACK_WEBHOOK_URL", "https://hooks.slack.com/services/T0/B0/XXX"
    )
    config = load_config(_EVAL_PATH)
    webhook = next(
        s.model_dump() for s in config.output if s.type == "webhook"
    )
    assert webhook["url"] == "https://hooks.slack.com/services/T0/B0/XXX"


def test_eval_reuses_regression_gate_agent_and_tiny_demo_cases(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Convention 8 — link, don't duplicate. The slack_drift_notify example
    must not ship its own agent.py or cases.yaml."""
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    example_dir = _EVAL_PATH.parent
    assert not (example_dir / "agent.py").exists()
    assert not (example_dir / "cases.yaml").exists()

    config = load_config(_EVAL_PATH)
    system = config.systems[0].model_dump()
    assert system["target"] == "examples.regression_gate.agent:run"
    assert config.dataset.path is not None
    assert config.dataset.path.endswith("examples/tiny_demo/cases.yaml")
