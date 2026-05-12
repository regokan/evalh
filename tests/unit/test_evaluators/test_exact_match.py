from __future__ import annotations

from collections.abc import Callable

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, ExpectedBehavior, Trace
from eval_harness.evaluators.exact_match import ExactMatchEvaluator

CaseFactory = Callable[..., EvalCase]
TraceFactory = Callable[..., Trace]


async def test_pass_when_field_equals_expected(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ExactMatchEvaluator.validate_config(
        {"field": "output.structured.price", "expected": 1200000}
    )
    ev = ExactMatchEvaluator(
        name="price_match", field="output.structured.price", expected=1200000
    )
    case = make_case()
    trace = make_trace(structured={"price": 1200000})

    result = await ev.evaluate(case, trace, None)
    assert result.passed
    assert result.detail["actual"] == 1200000
    assert result.detail["expected"] == 1200000
    assert result.started_at <= result.finished_at
    assert result.latency_ms >= 0


async def test_fail_when_values_differ(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = ExactMatchEvaluator(
        name="price_match", field="output.structured.price", expected=1200000
    )
    case = make_case()
    trace = make_trace(structured={"price": 999})

    result = await ev.evaluate(case, trace, None)
    assert not result.passed
    assert result.detail["actual"] == 999


async def test_expected_from_case_facts(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = ExactMatchEvaluator(
        name="price_from_truth",
        field="output.structured.price",
        expected_from="suburb_average_price",
    )
    expected_behavior = ExpectedBehavior(facts={"suburb_average_price": 1450000})
    case = make_case(expected=expected_behavior)
    trace = make_trace(structured={"price": 1450000})

    result = await ev.evaluate(case, trace, None)
    assert result.passed


async def test_validate_config_requires_field_and_expected() -> None:
    with pytest.raises(ConfigError, match="'field'"):
        ExactMatchEvaluator.validate_config({"expected": 1})
    with pytest.raises(ConfigError, match="expected"):
        ExactMatchEvaluator.validate_config({"field": "output.final_answer"})
