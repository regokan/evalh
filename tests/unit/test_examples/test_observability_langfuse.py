"""Smoke tests for the observability_langfuse example.

The example demonstrates the langfuse triplet (dataset/store/enricher) in
one config. These tests pin three contracts:

1. The shipped config loads without any langfuse env vars set.
2. The deterministic agent emits a Trace that the example's evaluators
   can grade.
3. The langfuse TraceStore, configured with empty api_key, no-ops cleanly
   instead of raising — this is what keeps the multi-sink `output:` list
   correct in both offline and online modes.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from eval_harness.adapters.trace.langfuse_trace_store import LangfuseTraceStore
from eval_harness.core.config_loader import load_config
from eval_harness.core.models import (
    EvaluationResult,
    Trace,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.core.time import utc_now
from examples.observability_langfuse.agent import run as agent_run

_EXAMPLE_DIR = Path(__file__).resolve().parents[3] / "examples" / "observability_langfuse"


def test_eval_yaml_loads_with_no_langfuse_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The config must parse cleanly when LANGFUSE_API_KEY / LANGFUSE_HOST are
    unset — that's the offline contract for the multi-sink output list."""
    monkeypatch.delenv("LANGFUSE_API_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    config = load_config(_EXAMPLE_DIR / "eval.yaml")
    sink_types = [sink.type for sink in config.output]
    assert sink_types == ["local_files", "langfuse"]


async def test_agent_returns_two_tool_calls_and_an_answer() -> None:
    """Pin the agent's output shape so the example's evaluators (`tool_called`
    + `contains_text`) stay in sync with what the agent emits."""
    case = {
        "input": {"user_message": "Tell me about ABC123.", "listing_id": "ABC123"},
        "metadata": {"suburb": "Richmond", "upstream_trace_id": "lf_abc123"},
    }
    out = await agent_run(case, {})
    assert "Richmond" in out["final_answer"]
    names = [c["name"] for c in out["tool_calls"]]
    assert names == ["get_listing_details", "get_average_suburb_price"]
    assert out["extra"]["trace_id"] == "lf_abc123"


async def test_langfuse_sink_noops_when_api_key_empty(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Empty api_key → store is `_disabled`, no SDK import attempted, save_*
    methods return without contacting Langfuse, and `open()` logs one warning."""
    store = LangfuseTraceStore(api_key="", host="")
    trace = Trace(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=utc_now(),
        finished_at=utc_now(),
        latency_ms=1,
        input={"user_message": "hi"},
        output=TraceOutput(final_answer="ok"),
        metrics=TraceMetrics(),
    )
    now = utc_now()
    result = EvaluationResult(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        evaluator="x",
        evaluator_type="contains_text",
        passed=True,
        score=None,
        reason="ok",
        started_at=now,
        finished_at=now,
        latency_ms=0,
    )
    with caplog.at_level(logging.WARNING):
        async with store:
            await store.open("r1", tmp_path)
            await store.save_trace(trace)
            await store.save_evaluation("c1", "v1", [result])
            assert await store.save_trace_idempotent(trace, "cell-1") is True

    assert any("LANGFUSE_API_KEY is unset" in r.getMessage() for r in caplog.records)
