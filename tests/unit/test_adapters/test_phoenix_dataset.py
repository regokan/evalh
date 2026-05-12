"""PhoenixDatasetAdapter tests.

Uses respx to play the Phoenix REST API. Skips when [phoenix] isn't
installed.
"""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
import respx

pytest.importorskip("phoenix.otel")

from eval_harness._platforms import otel as otel_mod
from eval_harness._platforms import phoenix as phoenix_mod
from eval_harness._platforms.phoenix import PhoenixClient
from eval_harness.adapters.dataset.phoenix_dataset_adapter import (
    PhoenixDatasetAdapter,
)
from eval_harness.core.errors import AdapterError, ConfigError


@pytest.fixture(autouse=True)
def _clean_registries() -> None:
    otel_mod._clear_registry_for_tests()
    phoenix_mod._clear_registry_for_tests()


@pytest.fixture
def respx_route() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


# ---- validate_config ----------------------------------------------------


def test_missing_base_url_raises() -> None:
    with pytest.raises(ConfigError, match="base_url"):
        PhoenixDatasetAdapter()


# ---- load_cases ---------------------------------------------------------


async def test_load_cases_returns_eval_cases(respx_route: respx.MockRouter) -> None:
    respx_route.get("http://phoenix:6006/v1/spans").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"id": "t1", "input": {"user_message": "hello"}},
                    {"id": "t2", "input": "world"},
                ]
            },
        )
    )
    client = PhoenixClient(base_url="http://phoenix:6006")
    adapter = PhoenixDatasetAdapter(
        base_url="http://phoenix:6006",
        client=client,
    )
    cases = await adapter.load_cases()
    assert [c.id for c in cases] == ["t1", "t2"]
    assert cases[0].input == {"user_message": "hello"}
    assert cases[1].input == {"user_message": "world"}
    # Provenance lands on metadata so `evalh inspect` can show source.
    assert cases[0].metadata["source"] == "phoenix"
    assert cases[0].metadata["trace_id"] == "t1"
    await client.aclose()


async def test_load_cases_sample_is_deterministic(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://phoenix:6006/v1/spans").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [{"id": f"t{i}", "input": f"q{i}"} for i in range(10)]
            },
        )
    )
    client = PhoenixClient(base_url="http://phoenix:6006")
    adapter = PhoenixDatasetAdapter(
        base_url="http://phoenix:6006",
        sample=3,
        filter={"start_time": "2026-05-01"},
        client=client,
    )
    a = [c.id for c in await adapter.load_cases()]
    b = [c.id for c in await adapter.load_cases()]
    assert a == b  # same filter -> same seed -> same sample
    assert len(a) == 3
    await client.aclose()


async def test_load_cases_search_failure_raises_adapter_error(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://phoenix:6006/v1/spans").mock(
        return_value=httpx.Response(500)
    )
    client = PhoenixClient(base_url="http://phoenix:6006")
    adapter = PhoenixDatasetAdapter(
        base_url="http://phoenix:6006", client=client
    )
    with pytest.raises(AdapterError, match="search_traces failed"):
        await adapter.load_cases()
    await client.aclose()


# ---- embed_full_trace --------------------------------------------------


async def test_embed_full_trace_attaches_replayable_trace(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://phoenix:6006/v1/spans").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "t1",
                        "input": {"user_message": "hi"},
                        "output": {"final_answer": "yo"},
                        "started_at": "2026-05-12T14:00:00+00:00",
                        "finished_at": "2026-05-12T14:00:01+00:00",
                        "metrics": {"token_input": 5, "token_output": 7},
                    }
                ]
            },
        )
    )
    client = PhoenixClient(base_url="http://phoenix:6006")
    adapter = PhoenixDatasetAdapter(
        base_url="http://phoenix:6006",
        embed_full_trace=True,
        client=client,
    )
    cases = await adapter.load_cases()
    assert len(cases) == 1
    case = cases[0]
    embedded = case._embedded_trace
    assert embedded is not None
    assert embedded.case_id == "t1"
    assert embedded.output.final_answer == "yo"
    assert embedded.metrics.token_input == 5
    assert embedded.extra["source_platform"] == "phoenix"
    assert embedded.extra["trace_id"] == "t1"
    await client.aclose()


async def test_embed_full_trace_off_by_default(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("http://phoenix:6006/v1/spans").mock(
        return_value=httpx.Response(
            200, json={"data": [{"id": "t1", "input": "hi"}]}
        )
    )
    client = PhoenixClient(base_url="http://phoenix:6006")
    adapter = PhoenixDatasetAdapter(
        base_url="http://phoenix:6006", client=client
    )
    cases = await adapter.load_cases()
    assert cases[0]._embedded_trace is None
    await client.aclose()


# ---- factory ------------------------------------------------------------


def test_factory_registers_phoenix_dataset() -> None:
    from eval_harness.factories import dataset_adapter_factory

    dataset_adapter_factory.load_entry_points()
    assert "phoenix" in dataset_adapter_factory.registry.names()
