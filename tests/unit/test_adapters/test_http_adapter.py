from __future__ import annotations

import httpx
import pytest
import respx

from eval_harness.adapters.system.http_adapter import HttpSystemAdapter
from eval_harness.core.errors import AdapterError, ConfigError, RetriableError
from eval_harness.core.models import EvalCase, RunVariant


def _case(case_id: str = "c1", user_message: str = "hello") -> EvalCase:
    return EvalCase(id=case_id, input={"user_message": user_message})


def _variant(name: str = "v1") -> RunVariant:
    return RunVariant(name=name, adapter="http", config={})


def test_http_adapter_rejects_file_scheme() -> None:
    with pytest.raises(ConfigError) as exc:
        HttpSystemAdapter(
            name="bad",
            endpoint="file:///etc/passwd",
            response_mapping={"final_answer": "$.x"},
        )
    assert "scheme" in str(exc.value).lower()


def test_http_adapter_rejects_gopher_scheme() -> None:
    with pytest.raises(ConfigError):
        HttpSystemAdapter(
            name="bad",
            endpoint="gopher://example.com/",
            response_mapping={"final_answer": "$.x"},
        )


def test_http_adapter_rejects_plain_http_to_remote() -> None:
    with pytest.raises(ConfigError):
        HttpSystemAdapter(
            name="bad",
            endpoint="http://example.com/api",
            response_mapping={"final_answer": "$.x"},
        )


def test_http_adapter_allows_https_and_localhost() -> None:
    a = HttpSystemAdapter(
        name="ok1",
        endpoint="https://api.example.com/",
        response_mapping={"final_answer": "$.x"},
    )
    b = HttpSystemAdapter(
        name="ok2",
        endpoint="http://localhost:8000/chat",
        response_mapping={"final_answer": "$.x"},
    )
    c = HttpSystemAdapter(
        name="ok3",
        endpoint="http://127.0.0.1:9000/chat",
        response_mapping={"final_answer": "$.x"},
    )
    assert a.name == "ok1" and b.name == "ok2" and c.name == "ok3"


def test_http_adapter_requires_response_mapping_without_provider() -> None:
    with pytest.raises(ConfigError):
        HttpSystemAdapter(name="x", endpoint="https://example.com/")


def test_http_adapter_unknown_provider_raises() -> None:
    with pytest.raises(ConfigError):
        HttpSystemAdapter(
            name="x",
            endpoint="https://example.com/",
            provider="nonsense",
        )


def test_http_adapter_stream_requires_format() -> None:
    from eval_harness.core.errors import ConfigError as _CE

    with pytest.raises(_CE):
        HttpSystemAdapter(
            name="x",
            endpoint="https://example.com/",
            stream=True,
        )


async def test_http_adapter_open_close_lifecycle() -> None:
    adapter = HttpSystemAdapter(
        name="x",
        endpoint="https://example.com/",
        response_mapping={"final_answer": "$.x"},
    )
    assert adapter._client is None  # type: ignore[reportPrivateUsage]
    async with adapter as live:
        assert live is adapter
        assert isinstance(adapter._client, httpx.AsyncClient)
    assert adapter._client is None


@respx.mock
async def test_http_adapter_response_mapping_extracts_fields() -> None:
    route = respx.post("https://api.example.com/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "answer": "the answer",
                "reasoning": "step by step",
                "tool_calls": [{"name": "lookup", "arguments": {"q": "hi"}}],
                "trace_id": "tr_abc",
                "usage": {"input": 11, "output": 22, "thinking": 33},
            },
        )
    )

    adapter = HttpSystemAdapter(
        name="agent",
        endpoint="https://api.example.com/chat",
        request_template='{"user_message": {{ input.user_message | json }}, '
        '"session_id": {{ case_id | json }}}',
        response_mapping={
            "final_answer": "$.answer",
            "thinking": "$.reasoning",
            "tool_calls": "$.tool_calls",
            "trace_id": "$.trace_id",
            "tokens.input": "$.usage.input",
            "tokens.output": "$.usage.output",
            "tokens.thinking": "$.usage.thinking",
        },
    )

    async with adapter:
        trace = await adapter.run(_case("case-1", "hi"), _variant("v1"), None)

    assert route.called
    assert trace.output.final_answer == "the answer"
    assert trace.output.thinking == "step by step"
    assert trace.metrics.token_input == 11
    assert trace.metrics.token_output == 22
    assert trace.metrics.token_thinking == 33
    assert trace.extra["trace_id"] == "tr_abc"
    assert len(trace.tool_calls) == 1
    assert trace.tool_calls[0].name == "lookup"
    assert trace.tool_calls[0].arguments == {"q": "hi"}
    assert trace.case_id == "case-1"
    assert trace.variant_name == "v1"
    assert trace.latency_ms >= 0


@respx.mock
async def test_http_adapter_thinking_never_concatenated_into_final_answer() -> None:
    """Thinking and final_answer must be stored as separate, distinct fields.

    Both come from different JSONPaths in the same response. The adapter must
    not collapse them together.
    """
    respx.post("https://api.example.com/chat").mock(
        return_value=httpx.Response(
            200,
            json={
                "answer": "FINAL_ONLY",
                "reasoning": "THINKING_ONLY",
            },
        )
    )

    adapter = HttpSystemAdapter(
        name="agent",
        endpoint="https://api.example.com/chat",
        response_mapping={
            "final_answer": "$.answer",
            "thinking": "$.reasoning",
        },
    )
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)

    assert trace.output.final_answer == "FINAL_ONLY"
    assert trace.output.thinking == "THINKING_ONLY"
    # No leakage.
    assert "THINKING_ONLY" not in (trace.output.final_answer or "")
    assert "FINAL_ONLY" not in (trace.output.thinking or "")


@respx.mock
async def test_http_adapter_timeout_becomes_retriable_error() -> None:
    respx.post("https://api.example.com/chat").mock(
        side_effect=httpx.TimeoutException("timed out")
    )
    adapter = HttpSystemAdapter(
        name="agent",
        endpoint="https://api.example.com/chat",
        response_mapping={"final_answer": "$.x"},
        timeout_seconds=1,
    )
    async with adapter:
        with pytest.raises(RetriableError):
            await adapter.run(_case(), _variant(), None)


@respx.mock
async def test_http_adapter_5xx_becomes_retriable_error() -> None:
    respx.post("https://api.example.com/chat").mock(
        return_value=httpx.Response(503, json={"error": "down"})
    )
    adapter = HttpSystemAdapter(
        name="agent",
        endpoint="https://api.example.com/chat",
        response_mapping={"final_answer": "$.x"},
    )
    async with adapter:
        with pytest.raises(RetriableError):
            await adapter.run(_case(), _variant(), None)


@respx.mock
async def test_http_adapter_4xx_becomes_adapter_error_not_retriable() -> None:
    respx.post("https://api.example.com/chat").mock(
        return_value=httpx.Response(400, json={"error": "bad"})
    )
    adapter = HttpSystemAdapter(
        name="agent",
        endpoint="https://api.example.com/chat",
        response_mapping={"final_answer": "$.x"},
    )
    async with adapter:
        with pytest.raises(AdapterError) as exc:
            await adapter.run(_case(), _variant(), None)
        assert not isinstance(exc.value, RetriableError)


@respx.mock
async def test_http_adapter_provider_preset_applied() -> None:
    """`provider:` preset fills request_template + response_mapping; user overrides win."""
    route = respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": "hello!",
                            "tool_calls": [],
                        }
                    }
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 7},
            },
        )
    )

    adapter = HttpSystemAdapter(
        name="oai",
        endpoint="https://api.openai.com/v1/chat/completions",
        provider="openai_chat",
    )
    async with adapter:
        trace = await adapter.run(_case(user_message="hi"), _variant(), None)

    assert route.called
    assert trace.output.final_answer == "hello!"
    assert trace.metrics.token_input == 5
    assert trace.metrics.token_output == 7


@respx.mock
async def test_http_adapter_user_response_mapping_overrides_provider_preset() -> None:
    respx.post("https://example.com/").mock(
        return_value=httpx.Response(200, json={"my_field": "X", "choices": []})
    )

    adapter = HttpSystemAdapter(
        name="custom",
        endpoint="https://example.com/",
        provider="openai_chat",
        response_mapping={"final_answer": "$.my_field"},
    )
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)

    assert trace.output.final_answer == "X"


async def test_http_adapter_run_outside_context_raises() -> None:
    adapter = HttpSystemAdapter(
        name="x",
        endpoint="https://example.com/",
        response_mapping={"final_answer": "$.x"},
    )
    with pytest.raises(AdapterError):
        await adapter.run(_case(), _variant(), None)


def test_http_adapter_enrich_trace_from_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    import logging

    caplog.set_level(logging.WARNING)
    HttpSystemAdapter(
        name="x",
        endpoint="https://example.com/",
        response_mapping={"final_answer": "$.x"},
        enrich_trace_from=[{"type": "langfuse"}],
    )
    assert any("enrich_trace_from" in rec.getMessage() for rec in caplog.records)
