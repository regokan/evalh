from __future__ import annotations

from collections.abc import Callable

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvalCase, Trace
from eval_harness.evaluators.schema_match import SchemaMatchEvaluator

CaseFactory = Callable[..., EvalCase]
TraceFactory = Callable[..., Trace]

_SCHEMA = {
    "type": "object",
    "properties": {
        "price": {"type": "number"},
        "suburb": {"type": "string"},
    },
    "required": ["price", "suburb"],
}


async def test_pass_when_structured_matches_schema(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    SchemaMatchEvaluator.validate_config({"schema": _SCHEMA})
    ev = SchemaMatchEvaluator(name="shape", schema=_SCHEMA)
    case = make_case()
    trace = make_trace(structured={"price": 1200000, "suburb": "Richmond"})

    result = await ev.evaluate(case, trace, None)
    assert result.passed
    assert result.detail["errors"] == []
    assert result.started_at <= result.finished_at
    assert result.latency_ms >= 0


async def test_fail_when_structured_violates_schema(
    make_case: CaseFactory, make_trace: TraceFactory
) -> None:
    ev = SchemaMatchEvaluator(name="shape", schema=_SCHEMA)
    case = make_case()
    trace = make_trace(structured={"price": "not-a-number"})  # also missing suburb

    result = await ev.evaluate(case, trace, None)
    assert not result.passed
    assert len(result.detail["errors"]) >= 1


async def test_validate_config_rejects_bad_schema() -> None:
    with pytest.raises(ConfigError, match="invalid JSON schema"):
        SchemaMatchEvaluator.validate_config({"schema": {"type": "no-such-type"}})


async def test_validate_config_requires_schema() -> None:
    with pytest.raises(ConfigError, match="schema"):
        SchemaMatchEvaluator.validate_config({})
