"""Tests for the BraintrustClient platform helper.

Uses a programmable in-memory FakeBraintrustSdk + a deterministic clock so
ingestion-lag scenarios are reproducible without ``time.sleep`` or a real
network. No real `braintrust` SDK import.
"""

from __future__ import annotations

from typing import Any

import pytest

from eval_harness._platforms.braintrust import (
    BraintrustClient,
    _clear_registry_for_tests,
    _registry_snapshot,
    get_or_create_braintrust_client,
    release_braintrust_client,
)
from eval_harness.core.errors import ConfigError


class _DeterministicClock:
    """Monotonic clock the test advances manually."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


class FakeBraintrustSdk:
    """Programmable in-memory Braintrust SDK shim. Stores pushed traces,
    returns listed traces, simulates ingestion lag via
    ``ingest_after_calls``."""

    def __init__(self) -> None:
        self.pushed: list[dict[str, Any]] = []
        self._traces: dict[str, dict[str, Any]] = {}
        self._fetch_calls: dict[str, int] = {}
        self.ingest_after_calls: dict[str, int] = {}
        self.search_calls: list[dict[str, Any]] = []
        self.flush_calls = 0
        self.shutdown_called = False

    def fetch_trace(self, trace_id: str) -> dict[str, Any] | None:
        self._fetch_calls[trace_id] = self._fetch_calls.get(trace_id, 0) + 1
        delay = self.ingest_after_calls.get(trace_id, 0)
        if self._fetch_calls[trace_id] <= delay:
            return None
        return self._traces.get(trace_id)

    def search_traces(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
        self.search_calls.append(filter)
        return [
            t
            for t in self._traces.values()
            if _matches(t, filter)
        ]

    def push_trace(self, trace: dict[str, Any]) -> None:
        self.pushed.append(trace)
        if trace.get("kind") == "trace":
            self._traces[trace["id"]] = trace

    def flush(self) -> None:
        self.flush_calls += 1

    def shutdown(self) -> None:
        self.shutdown_called = True

    def seed_trace(self, trace_id: str, payload: dict[str, Any]) -> None:
        self._traces[trace_id] = {"id": trace_id, **payload}


def _matches(trace: dict[str, Any], filter: dict[str, Any]) -> bool:
    """Trivial tag-match filter for the test fake."""
    tags = filter.get("tags")
    if isinstance(tags, list):
        trace_tags = trace.get("tags") or trace.get("metadata", {}).get("tags") or []
        return any(t in trace_tags for t in tags)
    return True


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    _clear_registry_for_tests()


async def test_construction_without_sdk_raises_when_braintrust_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the [braintrust] extra isn't installed, construction without an
    injected SDK raises ConfigError with the install hint."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "braintrust":
            raise ImportError("simulated missing braintrust")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ConfigError, match=r"eval-harness\[braintrust\]"):
        BraintrustClient(api_key="x", project="p")


async def test_search_traces_forwards_filter_and_returns_dicts() -> None:
    sdk = FakeBraintrustSdk()
    sdk.seed_trace("t1", {"tags": ["prod"], "input": {"q": 1}})
    sdk.seed_trace("t2", {"tags": ["staging"], "input": {"q": 2}})
    client = BraintrustClient(_sdk=sdk)
    out = await client.search_traces({"tags": ["prod"]})

    assert sdk.search_calls == [{"tags": ["prod"]}]
    assert len(out) == 1
    assert out[0]["id"] == "t1"


async def test_push_trace_forwards_payload() -> None:
    sdk = FakeBraintrustSdk()
    client = BraintrustClient(_sdk=sdk)
    await client.push_trace({"kind": "trace", "id": "abc", "name": "n"})
    assert sdk.pushed == [{"kind": "trace", "id": "abc", "name": "n"}]


async def test_fetch_trace_polls_until_ingested_then_returns() -> None:
    """The headline determinism test: ingestion-lag is driven by the fake +
    a manually-advanced clock + a counting fake sleeper. No real time
    elapses; the loop terminates only when the fake says the trace is
    visible."""
    sdk = FakeBraintrustSdk()
    sdk.seed_trace("late_trace", {"data": "hi"})
    sdk.ingest_after_calls["late_trace"] = 2

    clock = _DeterministicClock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    client = BraintrustClient(_sdk=sdk, clock=clock, sleeper=fake_sleep)
    out = await client.fetch_trace(
        "late_trace",
        wait_for_ingestion_seconds=10.0,
        poll_interval_seconds=0.5,
    )

    assert out is not None
    assert out["data"] == "hi"
    assert sdk._fetch_calls["late_trace"] == 3
    assert sleeps == [0.5, 0.5]


async def test_fetch_trace_returns_none_after_deadline() -> None:
    sdk = FakeBraintrustSdk()
    sdk.ingest_after_calls["missing"] = 100  # never appears

    clock = _DeterministicClock()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    client = BraintrustClient(_sdk=sdk, clock=clock, sleeper=fake_sleep)
    out = await client.fetch_trace(
        "missing", wait_for_ingestion_seconds=1.0, poll_interval_seconds=0.5
    )
    assert out is None
    assert sdk._fetch_calls["missing"] == 3
    assert len(slept) == 2


def test_registry_shares_client_across_acquires() -> None:
    sdk = FakeBraintrustSdk()
    a = get_or_create_braintrust_client(
        api_key="k", project="p", org="o", _sdk=sdk
    )
    b = get_or_create_braintrust_client(
        api_key="k", project="p", org="o", _sdk=sdk
    )
    try:
        assert a is b
        snap = _registry_snapshot()
        assert sum(snap.values()) == 2
    finally:
        release_braintrust_client(a)
        release_braintrust_client(b)
    assert _registry_snapshot() == {}
    assert sdk.shutdown_called


def test_registry_separate_clients_for_different_projects() -> None:
    sdk1 = FakeBraintrustSdk()
    sdk2 = FakeBraintrustSdk()
    a = get_or_create_braintrust_client(api_key="k", project="p1", _sdk=sdk1)
    b = get_or_create_braintrust_client(api_key="k", project="p2", _sdk=sdk2)
    try:
        assert a is not b
        assert len(_registry_snapshot()) == 2
    finally:
        release_braintrust_client(a)
        release_braintrust_client(b)


async def test_flush_forwards_to_sdk() -> None:
    sdk = FakeBraintrustSdk()
    client = BraintrustClient(_sdk=sdk)
    await client.flush()
    assert sdk.flush_calls == 1


async def test_methods_raise_after_shutdown() -> None:
    sdk = FakeBraintrustSdk()
    client = BraintrustClient(_sdk=sdk)
    client.shutdown()
    with pytest.raises(RuntimeError, match="after shutdown"):
        await client.search_traces({})
    with pytest.raises(RuntimeError, match="after shutdown"):
        await client.push_trace({})
    with pytest.raises(RuntimeError, match="after shutdown"):
        await client.fetch_trace("x")
