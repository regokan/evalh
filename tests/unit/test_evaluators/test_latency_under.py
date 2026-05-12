from __future__ import annotations

from collections.abc import Callable

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, Trace
from eval_harness.evaluators.latency_under import LatencyUnderEvaluator

CaseFactory = Callable[..., EvalCase]
TraceFactory = Callable[..., Trace]


async def test_pass_when_latency_below_threshold(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    LatencyUnderEvaluator.validate_config({"max_ms": 1000})
    ev = LatencyUnderEvaluator(name="fast", max_ms=1000)
    case = make_case()
    trace = make_trace()
    trace.latency_ms = 500

    result = await ev.evaluate(case, trace, None)
    assert result.passed
    assert result.detail == {"actual_ms": 500, "threshold_ms": 1000}


async def test_fail_when_latency_at_or_above_threshold(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = LatencyUnderEvaluator(name="fast", max_ms=1000)
    case = make_case()
    trace = make_trace()
    trace.latency_ms = 1500

    result = await ev.evaluate(case, trace, None)
    assert not result.passed
    assert ">= 1000ms" in result.reason


async def test_validate_config_requires_positive_max_ms() -> None:
    with pytest.raises(ConfigError, match="max_ms"):
        LatencyUnderEvaluator.validate_config({})
    with pytest.raises(ConfigError, match="max_ms"):
        LatencyUnderEvaluator.validate_config({"max_ms": 0})
    with pytest.raises(ConfigError, match="max_ms"):
        LatencyUnderEvaluator.validate_config({"max_ms": "fast"})
