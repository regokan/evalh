"""Runner-side replay invariants.

The runner overwrites Trace.started_at / finished_at / latency_ms with its own
wall-clock for every adapter EXCEPT replay. For replay traces those fields are
the upstream system's reality and must be preserved byte-for-byte.

These tests pin that contract at the function boundary
(`_enforce_invariants`) without spinning up a full runner.
"""

from __future__ import annotations

from datetime import UTC, datetime

from eval_harness.core.models import EvalCase, RunVariant, Trace, TraceOutput
from eval_harness.runner.run_eval import _enforce_invariants

_ORIG_START = datetime(2026, 5, 1, 10, 0, 0, tzinfo=UTC)
_ORIG_END = datetime(2026, 5, 1, 10, 0, 3, 142_000, tzinfo=UTC)
_ORIG_LATENCY_MS = 3142

_RUN_START = datetime(2026, 5, 12, 14, 0, 0, tzinfo=UTC)
_RUN_END = datetime(2026, 5, 12, 14, 0, 0, 50_000, tzinfo=UTC)


def _case() -> EvalCase:
    return EvalCase(id="c_now", input={})


def _variant() -> RunVariant:
    return RunVariant(name="v_now", adapter="replay", config={})


def _replay_trace() -> Trace:
    return Trace(
        run_id="upstream_run",
        case_id="upstream_case",
        variant_name="upstream_variant",
        started_at=_ORIG_START,
        finished_at=_ORIG_END,
        latency_ms=_ORIG_LATENCY_MS,
        input={},
        output=TraceOutput(final_answer="answer"),
        extra={"source": "replay", "replayed_from": {"platform": "langfuse"}},
    )


def test_enforce_invariants_preserves_replay_timestamps_and_latency() -> None:
    trace = _replay_trace()
    _enforce_invariants(
        trace, "current_run_id", _case(), _variant(), _RUN_START, _RUN_END
    )
    # Join keys updated.
    assert trace.run_id == "current_run_id"
    assert trace.case_id == "c_now"
    assert trace.variant_name == "v_now"
    # CRITICAL: timestamps + latency preserved exactly.
    assert trace.started_at == _ORIG_START
    assert trace.finished_at == _ORIG_END
    assert trace.latency_ms == _ORIG_LATENCY_MS


def test_enforce_invariants_overwrites_timestamps_for_non_replay() -> None:
    trace = Trace(
        run_id="x",
        case_id="x",
        variant_name="x",
        started_at=_ORIG_START,
        finished_at=_ORIG_END,
        latency_ms=_ORIG_LATENCY_MS,
        input={},
        output=TraceOutput(final_answer="answer"),
    )
    _enforce_invariants(
        trace, "current_run_id", _case(), _variant(), _RUN_START, _RUN_END
    )
    assert trace.started_at == _RUN_START
    assert trace.finished_at == _RUN_END
    # Wall-clock delta is 50ms.
    assert trace.latency_ms == 50


def test_enforce_invariants_overwrites_when_source_is_not_replay() -> None:
    trace = _replay_trace()
    trace.extra["source"] = "fresh"  # some other tag
    _enforce_invariants(
        trace, "current_run_id", _case(), _variant(), _RUN_START, _RUN_END
    )
    assert trace.started_at == _RUN_START
    assert trace.latency_ms == 50
