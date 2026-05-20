from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from eval_harness.adapters.system.python_function_adapter import PythonFunctionAdapter
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import EvalCase, RunVariant


def _case(case_id: str = "c1") -> EvalCase:
    return EvalCase(id=case_id, input={"user_message": "hello"})


def _variant(name: str = "v1") -> RunVariant:
    return RunVariant(name=name, adapter="python_function", config={})


def _install_module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def test_python_function_adapter_requires_target() -> None:
    with pytest.raises(ConfigError):
        PythonFunctionAdapter(name="x")


def test_python_function_adapter_target_must_have_colon() -> None:
    with pytest.raises(ConfigError):
        PythonFunctionAdapter(name="x", target="some.module.run")


async def test_python_function_adapter_unknown_module_raises_configerror() -> None:
    adapter = PythonFunctionAdapter(name="x", target="not_a_real_module_xyz:run")
    with pytest.raises(ConfigError):
        async with adapter:
            pass


async def test_python_function_adapter_unknown_attr_raises_configerror() -> None:
    _install_module("fake_pf_mod_1")
    adapter = PythonFunctionAdapter(name="x", target="fake_pf_mod_1:nope")
    with pytest.raises(ConfigError):
        async with adapter:
            pass


async def test_python_function_adapter_sync_target_call_shape() -> None:
    captured: dict[str, Any] = {}

    def my_agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        captured["case"] = case
        captured["variant"] = variant
        return {
            "final_answer": "sync-answer",
            "metrics": {"token_input": 3, "token_output": 4},
        }

    _install_module("fake_pf_mod_sync", run=my_agent)
    adapter = PythonFunctionAdapter(name="x", target="fake_pf_mod_sync:run")
    async with adapter:
        trace = await adapter.run(_case("c-sync"), _variant("v-sync"), None)

    assert captured["case"]["id"] == "c-sync"
    assert captured["case"]["input"] == {"user_message": "hello"}
    assert captured["variant"]["name"] == "v-sync"
    assert trace.output.final_answer == "sync-answer"
    assert trace.metrics.token_input == 3
    assert trace.metrics.token_output == 4
    assert trace.case_id == "c-sync"
    assert trace.variant_name == "v-sync"
    assert trace.latency_ms >= 0


async def test_python_function_adapter_async_target_call_shape() -> None:
    async def my_agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {
            "final_answer": "async-answer",
            "thinking": "considering...",
            "tool_calls": [{"name": "look", "arguments": {"q": "x"}}],
            "metrics": {"token_input": 1},
        }

    _install_module("fake_pf_mod_async", run=my_agent)
    adapter = PythonFunctionAdapter(name="x", target="fake_pf_mod_async:run")
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)

    assert trace.output.final_answer == "async-answer"
    assert trace.output.thinking == "considering..."
    assert len(trace.tool_calls) == 1
    assert trace.tool_calls[0].name == "look"
    assert trace.metrics.token_input == 1


async def test_python_function_adapter_thinking_never_concatenated() -> None:
    async def my_agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {"final_answer": "FA", "thinking": "TH"}

    _install_module("fake_pf_mod_think", run=my_agent)
    adapter = PythonFunctionAdapter(name="x", target="fake_pf_mod_think:run")
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    assert trace.output.final_answer == "FA"
    assert trace.output.thinking == "TH"
    assert "TH" not in (trace.output.final_answer or "")
    assert "FA" not in (trace.output.thinking or "")


async def test_python_function_adapter_target_exception_becomes_adapter_error() -> None:
    def boom(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("nope")

    _install_module("fake_pf_mod_boom", run=boom)
    adapter = PythonFunctionAdapter(name="x", target="fake_pf_mod_boom:run")
    async with adapter:
        with pytest.raises(AdapterError):
            await adapter.run(_case(), _variant(), None)


async def test_python_function_adapter_non_dict_return_raises() -> None:
    def bad(case: dict[str, Any], variant: dict[str, Any]) -> str:
        return "not a dict"

    _install_module("fake_pf_mod_bad_return", run=bad)
    adapter = PythonFunctionAdapter(name="x", target="fake_pf_mod_bad_return:run")
    async with adapter:
        with pytest.raises(AdapterError):
            await adapter.run(_case(), _variant(), None)


async def test_python_function_adapter_init_kwargs_instantiates_factory() -> None:
    class Agent:
        def __init__(self, model: str) -> None:
            self.model = model

        def __call__(self, case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
            return {"final_answer": f"called with {self.model}"}

    _install_module("fake_pf_mod_factory", Agent=Agent)
    adapter = PythonFunctionAdapter(
        name="x",
        target="fake_pf_mod_factory:Agent",
        init_kwargs={"model": "claude-haiku"},
    )
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    assert trace.output.final_answer == "called with claude-haiku"


async def test_python_function_adapter_propagates_structured() -> None:
    async def my_agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {
            "final_answer": "extracted",
            "structured": {"invoice_total": 1234.5, "currency": "USD"},
        }

    _install_module("fake_pf_mod_structured", run=my_agent)
    adapter = PythonFunctionAdapter(name="x", target="fake_pf_mod_structured:run")
    async with adapter:
        trace = await adapter.run(_case(), _variant(), None)
    assert trace.output.structured == {"invoice_total": 1234.5, "currency": "USD"}
    assert trace.output.final_answer == "extracted"


async def test_python_function_adapter_non_dict_structured_raises() -> None:
    def bad(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {"final_answer": "x", "structured": "not-a-dict"}

    _install_module("fake_pf_mod_structured_bad", run=bad)
    adapter = PythonFunctionAdapter(name="x", target="fake_pf_mod_structured_bad:run")
    async with adapter:
        with pytest.raises(AdapterError, match="structured"):
            await adapter.run(_case(), _variant(), None)


async def test_python_function_adapter_run_outside_context_raises() -> None:
    def my_agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        return {"final_answer": "x"}

    _install_module("fake_pf_mod_outside", run=my_agent)
    adapter = PythonFunctionAdapter(name="x", target="fake_pf_mod_outside:run")
    with pytest.raises(AdapterError):
        await adapter.run(_case(), _variant(), None)
