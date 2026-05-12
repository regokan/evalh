from __future__ import annotations

import random
import re
from collections.abc import Iterator
from typing import Any

import pytest

from eval_harness.core.llm_backends import LlmCall, llm_backend_registry

_ASSERTION_LINE_RE = re.compile(r"^\d+\.\s+\(")


class _FakeLlmBackend:
    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        schema: dict[str, Any] | None = None,
        cost_limit_usd: float | None = None,
    ) -> LlmCall:
        response: dict[str, Any] = {}
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if "assertions" in properties:
            count = sum(1 for line in prompt.splitlines() if _ASSERTION_LINE_RE.match(line))
            response["assertions"] = [
                {"text": f"assertion {i}", "passed": True, "reason": "fake-pass"}
                for i in range(count)
            ]
        if "score" in properties:
            response["score"] = 5.0
            response["rubric_reason"] = "fake-pass"
        return LlmCall(structured=response)


@pytest.fixture
def fake_judge_backend() -> Iterator[None]:
    prior_factories = dict(llm_backend_registry._factories)
    prior_instances = dict(llm_backend_registry._instances)
    backend = _FakeLlmBackend()
    llm_backend_registry.register("claude", lambda: backend)
    llm_backend_registry.register("gpt", lambda: backend)
    try:
        yield None
    finally:
        llm_backend_registry._factories = prior_factories
        llm_backend_registry._instances = prior_instances


@pytest.fixture(autouse=True)
def deterministic_rng() -> Iterator[None]:
    state = random.getstate()
    random.seed(42)
    try:
        yield None
    finally:
        random.setstate(state)
