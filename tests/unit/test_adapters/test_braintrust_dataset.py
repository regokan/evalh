"""BraintrustDatasetAdapter tests — fake SDK + deterministic clock."""

from __future__ import annotations

from typing import Any

import pytest

from eval_harness._platforms.braintrust import BraintrustClient
from eval_harness.adapters.dataset.braintrust_dataset_adapter import (
    BraintrustDatasetAdapter,
)
from eval_harness.core.models import Trace

# Re-use the fake from the platform test.
from tests.unit.test_platforms.test_braintrust import FakeBraintrustSdk


def _client_with_traces(traces: list[dict[str, Any]]) -> BraintrustClient:
    sdk = FakeBraintrustSdk()
    for t in traces:
        sdk.seed_trace(t["id"], t)
    return BraintrustClient(_sdk=sdk)


async def test_load_cases_maps_traces_to_evalcases() -> None:
    client = _client_with_traces(
        [
            {
                "id": "bt_001",
                "tags": ["production"],
                "input": {"user_message": "hi there"},
                "metadata": {"env": "prod"},
            },
            {
                "id": "bt_002",
                "tags": ["production"],
                "input": "what's the weather?",
                "metadata": {},
            },
        ]
    )
    adapter = BraintrustDatasetAdapter(
        filter={"tags": ["production"]}, client=client
    )
    cases = await adapter.load_cases()

    assert [c.id for c in cases] == ["bt_001", "bt_002"]
    assert cases[0].input == {"user_message": "hi there"}
    assert cases[1].input == {"user_message": "what's the weather?"}
    assert cases[0].metadata["source"] == "braintrust"
    assert cases[0].metadata["env"] == "prod"
    assert cases[0].metadata["trace_id"] == "bt_001"


async def test_load_cases_skips_embedded_trace_when_flag_false() -> None:
    client = _client_with_traces(
        [{"id": "bt_001", "tags": ["x"], "input": {"q": 1}, "output": "ans"}]
    )
    adapter = BraintrustDatasetAdapter(client=client)  # embed_full_trace=False
    cases = await adapter.load_cases()
    assert cases[0]._embedded_trace is None


async def test_load_cases_embeds_trace_when_flag_set() -> None:
    """With embed_full_trace=True the local Trace gets populated so the
    replay SystemAdapter can unwrap it later."""
    client = _client_with_traces(
        [
            {
                "id": "bt_010",
                "tags": ["x"],
                "input": {"user_message": "hello"},
                "output": {"final_answer": "hi back", "thinking": "considering..."},
                "started_at": "2026-05-01T10:00:00+00:00",
                "finished_at": "2026-05-01T10:00:03+00:00",
                "latency_ms": 3000,
                "metrics": {"token_input": 50, "token_output": 80, "cost_usd": 0.01},
                "messages": [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi back"},
                ],
            }
        ]
    )
    adapter = BraintrustDatasetAdapter(client=client, embed_full_trace=True)
    cases = await adapter.load_cases()

    embedded = cases[0]._embedded_trace
    assert isinstance(embedded, Trace)
    # Replay invariant: original timestamps, latency, metrics flow through.
    assert embedded.latency_ms == 3000
    assert embedded.metrics.token_input == 50
    assert embedded.metrics.cost_usd == 0.01
    assert embedded.output.final_answer == "hi back"
    assert embedded.output.thinking == "considering..."
    assert embedded.extra["trace_id"] == "bt_010"
    assert embedded.extra["source_platform"] == "braintrust"
    assert len(embedded.messages) == 2


async def test_load_cases_sample_limits_count_deterministically() -> None:
    traces = [
        {"id": f"bt_{i:03d}", "tags": ["p"], "input": {"q": i}}
        for i in range(20)
    ]
    client = _client_with_traces(traces)
    adapter = BraintrustDatasetAdapter(
        client=client, filter={"tags": ["p"]}, sample=5
    )
    a = await adapter.load_cases()
    adapter2 = BraintrustDatasetAdapter(
        client=client, filter={"tags": ["p"]}, sample=5
    )
    b = await adapter2.load_cases()
    assert len(a) == 5
    assert {c.id for c in a} == {c.id for c in b}


async def test_load_cases_surfaces_search_failure_as_adapter_error() -> None:
    class _Broken(FakeBraintrustSdk):
        def search_traces(self, filter: dict[str, Any]) -> list[dict[str, Any]]:
            raise RuntimeError("upstream is down")

    client = BraintrustClient(_sdk=_Broken())
    adapter = BraintrustDatasetAdapter(client=client)
    from eval_harness.core.errors import AdapterError

    with pytest.raises(AdapterError, match="search_traces failed"):
        await adapter.load_cases()


def test_factory_registers_braintrust_dataset() -> None:
    from eval_harness.factories import dataset_adapter_factory

    assert "braintrust" in dataset_adapter_factory.registry.names()


def test_factory_builds_with_client_passthrough() -> None:
    sdk = FakeBraintrustSdk()
    inst = BraintrustDatasetAdapter(
        client=BraintrustClient(_sdk=sdk), filter={}
    )
    assert isinstance(inst, BraintrustDatasetAdapter)
    assert inst.embed_full_trace is False


async def test_missing_id_field_raises() -> None:
    sdk = FakeBraintrustSdk()
    sdk._traces["bad"] = {"input": {"q": 1}}  # no id-like field
    client = BraintrustClient(_sdk=sdk)
    adapter = BraintrustDatasetAdapter(client=client)
    from eval_harness.core.errors import AdapterError

    with pytest.raises(AdapterError, match="no id-like field"):
        await adapter.load_cases()
