from __future__ import annotations

from collections.abc import Callable

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, Trace, TraceMetrics
from eval_harness.evaluators.cost_under import CostUnderEvaluator

CaseFactory = Callable[..., EvalCase]
TraceFactory = Callable[..., Trace]


async def test_pass_when_cost_below_threshold(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    CostUnderEvaluator.validate_config({"max_usd": 0.10})
    ev = CostUnderEvaluator(name="cheap", max_usd=0.10)
    case = make_case()
    trace = make_trace()
    trace.metrics = TraceMetrics(cost_usd=0.05)

    result = await ev.evaluate(case, trace, None)
    assert result.passed
    assert result.detail == {"actual_usd": 0.05, "threshold_usd": 0.10}


async def test_fail_when_cost_above_threshold(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = CostUnderEvaluator(name="cheap", max_usd=0.10)
    case = make_case()
    trace = make_trace()
    trace.metrics = TraceMetrics(cost_usd=0.50)

    result = await ev.evaluate(case, trace, None)
    assert not result.passed
    assert ">=" in result.reason


async def test_fail_when_cost_not_reported(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = CostUnderEvaluator(name="cheap", max_usd=0.10)
    case = make_case()
    trace = make_trace()  # default metrics have cost_usd=None

    result = await ev.evaluate(case, trace, None)
    assert not result.passed
    assert result.reason == "cost not reported by adapter"
    assert result.detail["actual_usd"] is None


async def test_validate_config_requires_positive_max_usd() -> None:
    with pytest.raises(ConfigError, match="max_usd"):
        CostUnderEvaluator.validate_config({})
    with pytest.raises(ConfigError, match="max_usd"):
        CostUnderEvaluator.validate_config({"max_usd": -1})
    with pytest.raises(ConfigError, match="max_usd"):
        CostUnderEvaluator.validate_config({"max_usd": "free"})
