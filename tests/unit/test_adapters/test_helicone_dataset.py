"""HeliconeDatasetAdapter tests.

Uses respx to play a fake Helicone API. No real network. The [helicone]
extra is a marker (httpx is core), so we don't `importorskip` — these
tests run in the default CI environment.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import httpx
import pytest
import respx

from eval_harness._platforms import helicone as helicone_mod
from eval_harness._platforms.helicone import HeliconeClient
from eval_harness.adapters.dataset.helicone_dataset_adapter import (
    HeliconeDatasetAdapter,
)
from eval_harness.core.errors import AdapterError, ConfigError


@pytest.fixture(autouse=True)
def _clean_registry() -> Iterator[None]:
    helicone_mod._clear_registry_for_tests()
    try:
        yield
    finally:
        helicone_mod._clear_registry_for_tests()


@pytest.fixture
def respx_route() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


def _helicone_log(
    request_id: str,
    *,
    user_message: str = "hello",
    answer: str | None = None,
    model: str = "claude-haiku-4-5",
    tokens_in: int = 12,
    tokens_out: int = 8,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "request_id": request_id,
        "model": model,
        "request_body": {
            "model": model,
            "messages": [{"role": "user", "content": user_message}],
        },
        "request_created_at": "2026-05-12T10:00:00Z",
        "response_created_at": "2026-05-12T10:00:01Z",
        "latency": 1100,
        "prompt_tokens": tokens_in,
        "completion_tokens": tokens_out,
        "cost": 0.0042,
    }
    if answer is not None:
        body["response_body"] = {
            "choices": [{"message": {"role": "assistant", "content": answer}}]
        }
    return body


# ---- HeliconeClient -----------------------------------------------------


async def test_client_requires_api_key() -> None:
    with pytest.raises(ConfigError, match="api_key"):
        HeliconeClient(api_key="")


async def test_client_sends_helicone_auth_header(
    respx_route: respx.MockRouter,
) -> None:
    route = respx_route.post("https://api.helicone.ai/v1/request/query").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = HeliconeClient(api_key="secret-key")
    await client.search_requests({"model": "claude-haiku-4-5"})
    sent = route.calls[0].request
    assert sent.headers["Helicone-Auth"] == "Bearer secret-key"
    assert sent.headers["Content-Type"].startswith("application/json")
    await client.aclose()


async def test_client_search_requests_unwraps_data_envelope(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.post("https://api.helicone.ai/v1/request/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [_helicone_log("r1"), _helicone_log("r2")],
            },
        )
    )
    client = HeliconeClient(api_key="k")
    out = await client.search_requests({})
    assert [r["request_id"] for r in out] == ["r1", "r2"]
    await client.aclose()


async def test_client_search_requests_handles_top_level_list(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.post("https://api.helicone.ai/v1/request/query").mock(
        return_value=httpx.Response(200, json=[_helicone_log("r1")])
    )
    client = HeliconeClient(api_key="k")
    out = await client.search_requests({})
    assert [r["request_id"] for r in out] == ["r1"]
    await client.aclose()


async def test_client_fetch_request_unwraps_envelope(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("https://api.helicone.ai/v1/request/r1").mock(
        return_value=httpx.Response(200, json={"data": _helicone_log("r1")})
    )
    client = HeliconeClient(api_key="k")
    payload = await client.fetch_request("r1")
    assert payload is not None
    assert payload["request_id"] == "r1"
    await client.aclose()


async def test_client_fetch_request_returns_none_on_404(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.get("https://api.helicone.ai/v1/request/missing").mock(
        return_value=httpx.Response(404)
    )
    client = HeliconeClient(api_key="k")
    assert await client.fetch_request("missing") is None
    await client.aclose()


async def test_client_custom_host_used(respx_route: respx.MockRouter) -> None:
    respx_route.post("https://helicone.internal/v1/request/query").mock(
        return_value=httpx.Response(200, json={"data": []})
    )
    client = HeliconeClient(api_key="k", host="https://helicone.internal/")
    out = await client.search_requests({})
    assert out == []
    assert client.host == "https://helicone.internal"
    await client.aclose()


# ---- Adapter — validation -----------------------------------------------


def test_adapter_requires_api_key_without_injected_client() -> None:
    with pytest.raises(ConfigError, match="api_key"):
        HeliconeDatasetAdapter()


# ---- Adapter — load_cases ----------------------------------------------


async def test_load_cases_maps_request_logs_to_cases(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.post("https://api.helicone.ai/v1/request/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _helicone_log("r1", user_message="hello"),
                    _helicone_log("r2", user_message="world"),
                ]
            },
        )
    )
    client = HeliconeClient(api_key="k")
    adapter = HeliconeDatasetAdapter(api_key="k", client=client)
    cases = await adapter.load_cases()
    assert [c.id for c in cases] == ["r1", "r2"]
    assert cases[0].input["user_message"] == "hello"
    # Provenance fields land on metadata.
    assert cases[0].metadata["source"] == "helicone"
    assert cases[0].metadata["trace_id"] == "r1"
    assert cases[0].metadata["model"] == "claude-haiku-4-5"
    await client.aclose()


async def test_load_cases_sample_is_deterministic(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.post("https://api.helicone.ai/v1/request/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _helicone_log(f"r{i}", user_message=f"q{i}") for i in range(10)
                ]
            },
        )
    )
    client = HeliconeClient(api_key="k")
    adapter = HeliconeDatasetAdapter(
        api_key="k",
        sample=3,
        filter={"model": "claude-haiku-4-5"},
        client=client,
    )
    a = [c.id for c in await adapter.load_cases()]
    b = [c.id for c in await adapter.load_cases()]
    assert a == b
    assert len(a) == 3
    await client.aclose()


async def test_load_cases_search_failure_raises_adapter_error(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.post("https://api.helicone.ai/v1/request/query").mock(
        return_value=httpx.Response(500)
    )
    client = HeliconeClient(api_key="k")
    adapter = HeliconeDatasetAdapter(api_key="k", client=client)
    with pytest.raises(AdapterError, match="search_requests failed"):
        await adapter.load_cases()
    await client.aclose()


async def test_load_cases_skips_no_id_logs() -> None:
    """A request log with no id fields should raise — silently dropping
    would let bad upstream data poison the case stream invisibly."""
    bad = {"request_body": {"messages": [{"role": "user", "content": "hi"}]}}
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        router.post("https://api.helicone.ai/v1/request/query").mock(
            return_value=httpx.Response(200, json={"data": [bad]})
        )
        client = HeliconeClient(api_key="k")
        adapter = HeliconeDatasetAdapter(api_key="k", client=client)
        with pytest.raises(AdapterError, match="no id-like field"):
            await adapter.load_cases()
        await client.aclose()


# ---- Adapter — embed_full_trace ----------------------------------------


async def test_embed_full_trace_attaches_replayable_trace(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.post("https://api.helicone.ai/v1/request/query").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    _helicone_log(
                        "r1", user_message="ping", answer="pong", tokens_in=5, tokens_out=7
                    )
                ]
            },
        )
    )
    client = HeliconeClient(api_key="k")
    adapter = HeliconeDatasetAdapter(
        api_key="k", embed_full_trace=True, client=client
    )
    cases = await adapter.load_cases()
    assert len(cases) == 1
    embedded = cases[0]._embedded_trace
    assert embedded is not None
    assert embedded.case_id == "r1"
    assert embedded.output.final_answer == "pong"
    assert embedded.metrics.token_input == 5
    assert embedded.metrics.token_output == 7
    assert embedded.metrics.cost_usd == pytest.approx(0.0042)
    assert embedded.extra["source_platform"] == "helicone"
    assert embedded.extra["trace_id"] == "r1"
    # Latency picked up from the request log.
    assert embedded.latency_ms == 1100
    await client.aclose()


async def test_embed_full_trace_off_by_default(
    respx_route: respx.MockRouter,
) -> None:
    respx_route.post("https://api.helicone.ai/v1/request/query").mock(
        return_value=httpx.Response(200, json={"data": [_helicone_log("r1")]})
    )
    client = HeliconeClient(api_key="k")
    adapter = HeliconeDatasetAdapter(api_key="k", client=client)
    cases = await adapter.load_cases()
    assert cases[0]._embedded_trace is None
    await client.aclose()


# ---- Factory registration ----------------------------------------------


def test_factory_registers_helicone() -> None:
    from eval_harness.factories import dataset_adapter_factory

    dataset_adapter_factory.load_entry_points()
    assert "helicone" in dataset_adapter_factory.registry.names()
