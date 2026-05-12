from __future__ import annotations

from collections.abc import Callable

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, ExpectedBehavior, Trace
from eval_harness.evaluators.contains_text import ContainsTextEvaluator

CaseFactory = Callable[..., EvalCase]
TraceFactory = Callable[..., Trace]


async def test_pass_when_all_required_substrings_present(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ContainsTextEvaluator.validate_config({"all_of": ["Richmond"], "none_of": ["error"]})
    ev = ContainsTextEvaluator(name="answer_mentions", all_of=["Richmond"], none_of=["error"])
    case = make_case()
    trace = make_trace(final_answer="The average in Richmond is $1.2M.")

    result = await ev.evaluate(case, trace, None)

    assert result.passed
    assert result.evaluator == "answer_mentions"
    assert result.evaluator_type == "contains_text"
    assert "Richmond" in result.detail["matched_all_of"]
    assert result.started_at <= result.finished_at
    assert result.latency_ms >= 0


async def test_fail_when_forbidden_substring_present(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = ContainsTextEvaluator(name="no_apology", none_of=["sorry"])
    case = make_case()
    trace = make_trace(final_answer="Sorry I cannot help.")

    result = await ev.evaluate(case, trace, None)

    assert not result.passed
    assert "forbidden present" in result.reason


async def test_field_targets_thinking(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    # Per docs/Evaluators.md > Evaluating thinking — field: output.thinking must work.
    ev = ContainsTextEvaluator(
        name="no_loops", field="output.thinking", none_of=["Let me reconsider"]
    )
    case = make_case()
    trace = make_trace(
        final_answer="Final.", thinking="Let me reconsider this entirely."
    )

    result = await ev.evaluate(case, trace, None)
    assert not result.passed
    assert "Let me reconsider" in result.detail["matched_none_of"]


async def test_validate_config_rejects_bad_types() -> None:
    with pytest.raises(ConfigError):
        ContainsTextEvaluator.validate_config({"all_of": "not-a-list"})


async def test_falls_back_to_case_expected(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    expected = ExpectedBehavior(
        answer_should_include=["Carlton"], answer_should_not_include=["error"]
    )
    ev = ContainsTextEvaluator(name="from_case")
    case = make_case(expected=expected)
    trace = make_trace(final_answer="The Carlton suburb is great.")
    result = await ev.evaluate(case, trace, None)
    assert result.passed
    assert result.detail["matched_all_of"] == ["Carlton"]
