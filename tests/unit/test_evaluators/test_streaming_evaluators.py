"""Tests for the three streaming-only evaluators (ev-xao).

Each evaluator gets one pass, one fail, and one error/sentinel case — the
"error" case here is the streaming-metric-absent scenario (the metric the
evaluator reads is ``None`` because the system didn't stream).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    Trace,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.evaluators.latency_first_token_under import (
    LatencyFirstTokenUnderEvaluator,
)
from eval_harness.evaluators.stream_completed import StreamCompletedEvaluator
from eval_harness.evaluators.tokens_per_second_above import (
    TokensPerSecondAboveEvaluator,
)

_NOW = datetime(2026, 5, 12, 12, 0, 0, tzinfo=UTC)


def _trace(
    *,
    latency_first_token_ms: int | None = None,
    tokens_per_second: float | None = None,
    stream_completed: bool | None = None,
) -> Trace:
    return Trace(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=0,
        input={},
        output=TraceOutput(final_answer="x"),
        metrics=TraceMetrics(
            latency_first_token_ms=latency_first_token_ms,
            tokens_per_second=tokens_per_second,
            stream_completed=stream_completed,
        ),
    )


def _case() -> EvalCase:
    return EvalCase(id="c1", input={})


# --- latency_first_token_under -------------------------------------------


async def test_latency_first_token_under_pass() -> None:
    ev = LatencyFirstTokenUnderEvaluator(name="ttft", max_ms=500)
    res = await ev.evaluate(_case(), _trace(latency_first_token_ms=210), None)
    assert res.passed is True
    assert "210ms < 500ms" in res.reason
    assert res.detail["actual_ms"] == 210
    assert res.detail["threshold_ms"] == 500


async def test_latency_first_token_under_fail_when_over_threshold() -> None:
    ev = LatencyFirstTokenUnderEvaluator(name="ttft", max_ms=200)
    res = await ev.evaluate(_case(), _trace(latency_first_token_ms=350), None)
    assert res.passed is False
    assert "350ms >= 200ms" in res.reason


async def test_latency_first_token_under_fail_when_metric_absent() -> None:
    ev = LatencyFirstTokenUnderEvaluator(name="ttft", max_ms=500)
    res = await ev.evaluate(_case(), _trace(), None)  # no streaming metric
    assert res.passed is False
    assert res.reason == "not a streaming system"
    assert res.detail["actual_ms"] is None


def test_latency_first_token_under_validate_rejects_bad_config() -> None:
    with pytest.raises(ConfigError, match="positive int"):
        LatencyFirstTokenUnderEvaluator.validate_config({})
    with pytest.raises(ConfigError):
        LatencyFirstTokenUnderEvaluator.validate_config({"max_ms": 0})
    with pytest.raises(ConfigError):
        LatencyFirstTokenUnderEvaluator.validate_config({"max_ms": True})


# --- tokens_per_second_above ---------------------------------------------


async def test_tokens_per_second_above_pass() -> None:
    ev = TokensPerSecondAboveEvaluator(name="tps", min_tps=20)
    res = await ev.evaluate(_case(), _trace(tokens_per_second=42.5), None)
    assert res.passed is True
    assert "42.50 >= 20.00" in res.reason
    assert res.detail["actual_tps"] == 42.5


async def test_tokens_per_second_above_fail_when_below_threshold() -> None:
    ev = TokensPerSecondAboveEvaluator(name="tps", min_tps=50)
    res = await ev.evaluate(_case(), _trace(tokens_per_second=15.0), None)
    assert res.passed is False
    assert "15.00 < 50.00" in res.reason


async def test_tokens_per_second_above_fail_when_metric_absent() -> None:
    ev = TokensPerSecondAboveEvaluator(name="tps", min_tps=20)
    res = await ev.evaluate(_case(), _trace(), None)
    assert res.passed is False
    assert res.reason == "not streaming"


def test_tokens_per_second_above_validate_rejects_bad_config() -> None:
    with pytest.raises(ConfigError, match="positive number"):
        TokensPerSecondAboveEvaluator.validate_config({})
    with pytest.raises(ConfigError):
        TokensPerSecondAboveEvaluator.validate_config({"min_tps": 0})
    with pytest.raises(ConfigError):
        TokensPerSecondAboveEvaluator.validate_config({"min_tps": True})


# --- stream_completed ----------------------------------------------------


async def test_stream_completed_pass_when_true() -> None:
    ev = StreamCompletedEvaluator(name="sc")
    res = await ev.evaluate(_case(), _trace(stream_completed=True), None)
    assert res.passed is True
    assert res.reason == "stream completed"
    assert res.detail["stream_completed"] is True


async def test_stream_completed_fail_when_false() -> None:
    ev = StreamCompletedEvaluator(name="sc")
    res = await ev.evaluate(_case(), _trace(stream_completed=False), None)
    assert res.passed is False
    assert res.reason == "stream truncated"


async def test_stream_completed_fail_when_metric_absent() -> None:
    ev = StreamCompletedEvaluator(name="sc")
    res = await ev.evaluate(_case(), _trace(), None)
    assert res.passed is False
    assert res.reason == "not streaming"


# --- factory wiring sanity ----------------------------------------------


def test_factory_registers_three_streaming_evaluators() -> None:
    from eval_harness.factories import evaluator_factory

    names = evaluator_factory.registry.names()
    assert "latency_first_token_under" in names
    assert "tokens_per_second_above" in names
    assert "stream_completed" in names
