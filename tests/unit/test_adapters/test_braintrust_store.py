"""BraintrustTraceStore tests — verifies push-shape + sink-error semantics."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from eval_harness._platforms.braintrust import BraintrustClient
from eval_harness.adapters.trace.braintrust_trace_store import BraintrustTraceStore
from eval_harness.core.errors import AdapterError
from eval_harness.core.models import (
    EvaluationResult,
    RunSummary,
    Trace,
    TraceMetrics,
    TraceOutput,
)
from tests.unit.test_platforms.test_braintrust import FakeBraintrustSdk

_NOW = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)


def _trace(
    case: str = "c1", variant: str = "v1", trace_id: str | None = None
) -> Trace:
    return Trace(
        run_id="r1",
        case_id=case,
        variant_name=variant,
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=42,
        input={"user_message": "hi"},
        output=TraceOutput(final_answer="hello"),
        metrics=TraceMetrics(token_input=10, token_output=20, cost_usd=0.005),
        extra={"trace_id": trace_id} if trace_id else {},
    )


def _result(
    case: str, ev: str, passed: bool, score: float | None = None
) -> EvaluationResult:
    return EvaluationResult(
        run_id="r1",
        case_id=case,
        variant_name="v1",
        evaluator=ev,
        evaluator_type="x",
        passed=passed,
        score=score,
        reason="ok" if passed else "no",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
    )


async def test_save_trace_pushes_canonical_payload(tmp_path: Path) -> None:
    sdk = FakeBraintrustSdk()
    client = BraintrustClient(_sdk=sdk)
    store = BraintrustTraceStore(client=client)
    async with store:
        await store.open("r1", tmp_path)
        await store.save_trace(_trace(trace_id="bt_abc"))

    assert len(sdk.pushed) == 1
    p = sdk.pushed[0]
    assert p["kind"] == "trace"
    assert p["id"] == "bt_abc"
    assert p["run_id"] == "r1"
    assert p["case_id"] == "c1"
    assert p["variant_name"] == "v1"
    assert p["input"] == {"user_message": "hi"}
    assert p["output"]["final_answer"] == "hello"
    assert p["metrics"]["token_input"] == 10
    assert p["latency_ms"] == 42


async def test_save_trace_synthesises_id_when_upstream_id_missing(
    tmp_path: Path,
) -> None:
    sdk = FakeBraintrustSdk()
    client = BraintrustClient(_sdk=sdk)
    store = BraintrustTraceStore(client=client)
    async with store:
        await store.open("r1", tmp_path)
        await store.save_trace(_trace())  # no extra.trace_id
    assert sdk.pushed[0]["id"] == "r1__c1__v1"


async def test_save_trace_before_open_raises(tmp_path: Path) -> None:
    sdk = FakeBraintrustSdk()
    client = BraintrustClient(_sdk=sdk)
    store = BraintrustTraceStore(client=client)
    async with store:
        with pytest.raises(AdapterError, match="before open"):
            await store.save_trace(_trace())


async def test_save_evaluation_pushes_one_scores_row_per_call(
    tmp_path: Path,
) -> None:
    sdk = FakeBraintrustSdk()
    client = BraintrustClient(_sdk=sdk)
    store = BraintrustTraceStore(client=client)
    async with store:
        await store.open("r1", tmp_path)
        await store.save_evaluation(
            "c1",
            "v1",
            [
                _result("c1", "ev_a", True, 1.0),
                _result("c1", "ev_b", False, 0.0),
            ],
        )

    assert len(sdk.pushed) == 1
    p = sdk.pushed[0]
    assert p["kind"] == "scores"
    assert p["case_id"] == "c1"
    assert [s["evaluator"] for s in p["scores"]] == ["ev_a", "ev_b"]
    assert [s["passed"] for s in p["scores"]] == [True, False]


async def test_save_evaluation_with_empty_list_pushes_nothing(
    tmp_path: Path,
) -> None:
    sdk = FakeBraintrustSdk()
    client = BraintrustClient(_sdk=sdk)
    store = BraintrustTraceStore(client=client)
    async with store:
        await store.open("r1", tmp_path)
        await store.save_evaluation("c1", "v1", [])
    assert sdk.pushed == []


async def test_save_summary_and_save_artifact_are_noops(tmp_path: Path) -> None:
    sdk = FakeBraintrustSdk()
    client = BraintrustClient(_sdk=sdk)
    store = BraintrustTraceStore(client=client)
    async with store:
        await store.open("r1", tmp_path)
        await store.save_summary(
            RunSummary(
                run_id="r1",
                started_at=_NOW,
                finished_at=_NOW,
                config_path="x",
                config_hash="",
                cases_total=0,
                variants=[],
                by_evaluator=[],
            )
        )
    assert sdk.pushed == []


async def test_exit_flushes_sdk(tmp_path: Path) -> None:
    sdk = FakeBraintrustSdk()
    client = BraintrustClient(_sdk=sdk)
    store = BraintrustTraceStore(client=client)
    async with store:
        await store.open("r1", tmp_path)
        await store.save_trace(_trace())
    assert sdk.flush_calls == 1


async def test_store_failure_propagates_for_sink_error_recording(
    tmp_path: Path,
) -> None:
    """The store raises cleanly on SDK push failure; the runner's
    multi-sink dispatcher (ev-7aj) decides whether to abort (first-sink)
    or record into ``RunSummary.sink_errors`` (mirror)."""

    class _BrokenSdk(FakeBraintrustSdk):
        def push_trace(self, trace: dict[str, Any]) -> None:
            raise RuntimeError("braintrust 502")

    client = BraintrustClient(_sdk=_BrokenSdk())
    store = BraintrustTraceStore(client=client)
    async with store:
        await store.open("r1", tmp_path)
        with pytest.raises(RuntimeError, match="braintrust 502"):
            await store.save_trace(_trace())


def test_factory_registers_braintrust_store() -> None:
    from eval_harness.factories import trace_store_factory

    assert "braintrust" in trace_store_factory.registry.names()
