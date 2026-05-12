"""Tests for the LangfuseClient platform helper.

Uses a programmable in-memory FakeLangfuseSdk + a deterministic clock so
ingestion-lag scenarios are reproducible without ``time.sleep`` or a real
network. No real `langfuse` SDK import.
"""

from __future__ import annotations

from typing import Any

import pytest

from eval_harness._platforms.langfuse import (
    LangfuseClient,
    _clear_registry_for_tests,
    _registry_snapshot,
    get_or_create_langfuse_client,
    release_langfuse_client,
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


class FakeLangfuseSdk:
    """Programmable in-memory Langfuse: stores pushed traces, returns
    listed traces, simulates ingestion lag via ``ingest_after_calls``.

    The shim the production code uses calls our methods 1:1, so this fake
    satisfies the same shape with no real SDK installed.
    """

    def __init__(self) -> None:
        self.pushed: list[dict[str, Any]] = []
        self._traces: dict[str, dict[str, Any]] = {}
        self._fetch_calls: dict[str, int] = {}
        # trace_id -> N: returns None for the first N fetch_trace calls then
        # the stored trace. Used to assert poll-until-ingested behaviour.
        self.ingest_after_calls: dict[str, int] = {}
        self.search_calls: list[dict[str, Any]] = []
        self.flush_calls = 0
        self.shutdown_called = False

    # API used by LangfuseClient -----------------------------------------
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

    # Test seeding -------------------------------------------------------
    def seed_trace(self, trace_id: str, payload: dict[str, Any]) -> None:
        self._traces[trace_id] = {"id": trace_id, **payload}


def _matches(trace: dict[str, Any], filter: dict[str, Any]) -> bool:
    """Trivial tag-match filter; the production Langfuse SDK is richer but
    these tests don't exercise that."""
    tags = filter.get("tags")
    if isinstance(tags, list):
        trace_tags = trace.get("tags") or trace.get("metadata", {}).get("tags") or []
        return any(t in trace_tags for t in tags)
    return True


@pytest.fixture(autouse=True)
def _reset_registry() -> None:
    _clear_registry_for_tests()


async def test_construction_without_sdk_raises_when_langfuse_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the [langfuse] extra isn't installed, construction without an
    injected SDK raises ConfigError with the install hint."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "langfuse":
            raise ImportError("simulated missing langfuse")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ConfigError, match=r"eval-harness\[langfuse\]"):
        LangfuseClient(api_key="x", host="https://h")


async def test_search_traces_forwards_filter_and_returns_dicts() -> None:
    sdk = FakeLangfuseSdk()
    sdk.seed_trace("t1", {"tags": ["prod"], "input": {"q": 1}})
    sdk.seed_trace("t2", {"tags": ["staging"], "input": {"q": 2}})

    client = LangfuseClient(_sdk=sdk)
    out = await client.search_traces({"tags": ["prod"]})

    assert sdk.search_calls == [{"tags": ["prod"]}]
    assert len(out) == 1
    assert out[0]["id"] == "t1"


async def test_push_trace_forwards_payload() -> None:
    sdk = FakeLangfuseSdk()
    client = LangfuseClient(_sdk=sdk)
    await client.push_trace({"kind": "trace", "id": "abc", "name": "n"})
    assert sdk.pushed == [{"kind": "trace", "id": "abc", "name": "n"}]


async def test_fetch_trace_polls_until_ingested_then_returns() -> None:
    """The headline determinism test: ingestion-lag is driven by the fake +
    a manually-advanced clock + a counting fake sleeper. No real time
    elapses; the loop terminates only when the fake says the trace is
    visible."""
    sdk = FakeLangfuseSdk()
    sdk.seed_trace("late_trace", {"data": "hi"})
    sdk.ingest_after_calls["late_trace"] = 2  # first two fetches return None

    clock = _DeterministicClock()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock.advance(seconds)

    client = LangfuseClient(_sdk=sdk, clock=clock, sleeper=fake_sleep)
    out = await client.fetch_trace(
        "late_trace",
        wait_for_ingestion_seconds=10.0,
        poll_interval_seconds=0.5,
    )

    assert out is not None
    assert out["data"] == "hi"
    # 3 attempts: 2 misses + 1 hit. 2 sleeps between attempts.
    assert sdk._fetch_calls["late_trace"] == 3
    assert sleeps == [0.5, 0.5]


async def test_fetch_trace_returns_none_after_deadline() -> None:
    sdk = FakeLangfuseSdk()
    sdk.ingest_after_calls["missing"] = 100  # never appears

    clock = _DeterministicClock()
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    client = LangfuseClient(_sdk=sdk, clock=clock, sleeper=fake_sleep)
    out = await client.fetch_trace(
        "missing", wait_for_ingestion_seconds=1.0, poll_interval_seconds=0.5
    )

    assert out is None
    # Deadline = 1.0s. Poll interval 0.5s.
    # attempt 1 at t=0 -> miss -> sleep 0.5 -> t=0.5
    # attempt 2 at t=0.5 -> miss -> sleep 0.5 -> t=1.0
    # attempt 3 at t=1.0 -> miss; clock now 1.0 == deadline -> return None.
    assert sdk._fetch_calls["missing"] == 3
    # No more polling beyond the deadline.
    assert len(slept) == 2


def test_registry_shares_client_across_acquires() -> None:
    sdk = FakeLangfuseSdk()
    a = get_or_create_langfuse_client(api_key="k", host="h", _sdk=sdk)
    b = get_or_create_langfuse_client(api_key="k", host="h", _sdk=sdk)
    try:
        assert a is b
        assert _registry_snapshot() == {
            '{"api_key": "k", "host": "h"}': 2
        }
    finally:
        release_langfuse_client(a)
        release_langfuse_client(b)
    # Both releases drop the refcount to zero -> registry empty.
    assert _registry_snapshot() == {}
    # The SDK got shut down on last release.
    assert sdk.shutdown_called


def test_registry_separate_clients_for_different_hosts() -> None:
    sdk1 = FakeLangfuseSdk()
    sdk2 = FakeLangfuseSdk()
    a = get_or_create_langfuse_client(api_key="k", host="h1", _sdk=sdk1)
    b = get_or_create_langfuse_client(api_key="k", host="h2", _sdk=sdk2)
    try:
        assert a is not b
        assert len(_registry_snapshot()) == 2
    finally:
        release_langfuse_client(a)
        release_langfuse_client(b)


async def test_flush_forwards_to_sdk() -> None:
    sdk = FakeLangfuseSdk()
    client = LangfuseClient(_sdk=sdk)
    await client.flush()
    assert sdk.flush_calls == 1


async def test_methods_raise_after_shutdown() -> None:
    sdk = FakeLangfuseSdk()
    client = LangfuseClient(_sdk=sdk)
    client.shutdown()
    with pytest.raises(RuntimeError, match="after shutdown"):
        await client.search_traces({})
    with pytest.raises(RuntimeError, match="after shutdown"):
        await client.push_trace({})
    with pytest.raises(RuntimeError, match="after shutdown"):
        await client.fetch_trace("x")
