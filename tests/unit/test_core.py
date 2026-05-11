from __future__ import annotations

import pytest

from eval_harness.core.errors import AdapterError, ConfigError, RetriableError
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    RunSummary,
    Trace,
    TraceOutput,
)
from eval_harness.core.registry import Registry
from eval_harness.core.time import make_run_id, utc_now


def test_retriable_error_is_adapter_error() -> None:
    assert issubclass(RetriableError, AdapterError)


def test_trace_from_error_populates_error_and_zero_latency() -> None:
    t = Trace.from_error("c", "v", "timeout", "msg")
    assert t.case_id == "c"
    assert t.variant_name == "v"
    assert t.error is not None
    assert t.error.type == "timeout"
    assert t.error.message == "msg"
    assert t.latency_ms == 0
    assert t.started_at == t.finished_at
    assert t.schema_version == "1.0"


def test_evaluation_result_from_error_normalizes_crash() -> None:
    try:
        raise ValueError("boom")
    except ValueError as e:
        r = EvaluationResult.from_error("my_eval", e)
    assert r.evaluator == "my_eval"
    assert r.passed is False
    assert r.latency_ms == 0
    assert r.error is not None
    assert r.error.type == "ValueError"
    assert "boom" in r.error.message


def test_eval_case_defaults() -> None:
    c = EvalCase(id="c1", input={"msg": "hi"})
    assert c.schema_version == "1.0"
    assert c.expected.must_call_tools == []
    assert c.metadata == {}


def test_run_summary_constructs() -> None:
    now = utc_now()
    s = RunSummary(
        run_id="r1",
        started_at=now,
        finished_at=now,
        config_path="x.yaml",
        config_hash="abc",
        cases_total=0,
        variants=[],
        by_evaluator=[],
    )
    assert s.schema_version == "1.0"
    assert s.comparison is None


def test_trace_output_optional_fields() -> None:
    o = TraceOutput()
    assert o.final_answer is None
    assert o.thinking is None
    assert o.structured is None


def test_registry_unknown_raises_configerror_listing_names() -> None:
    reg: Registry[type] = Registry("evaluator")
    reg.register("foo", str)
    reg.register("bar", int)
    with pytest.raises(ConfigError) as exc_info:
        reg.get("missing")
    msg = str(exc_info.value)
    assert "evaluator" in msg
    assert "missing" in msg
    assert "foo" in msg
    assert "bar" in msg


def test_registry_get_returns_registered_class() -> None:
    reg: Registry[type] = Registry("widget")
    reg.register("alpha", list)
    assert reg.get("alpha") is list


def test_registry_names_sorted() -> None:
    reg: Registry[type] = Registry("widget")
    reg.register("zeta", list)
    reg.register("alpha", dict)
    assert reg.names() == ["alpha", "zeta"]


def test_make_run_id_replaces_colons() -> None:
    rid = make_run_id("listing_price_eval")
    assert ":" not in rid
    assert rid.endswith("_listing_price_eval")
    # Format: YYYY-MM-DDTHH-MM-SS_name
    assert rid[4] == "-"
    assert rid[10] == "T"


def test_utc_now_is_tz_aware_utc() -> None:
    n = utc_now()
    assert n.tzinfo is not None
    assert n.utcoffset() is not None
    offset = n.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0
