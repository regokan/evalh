from __future__ import annotations

import random
import re
from collections.abc import Iterator
from typing import Any

import pytest

from eval_harness.evaluators._judge_backends import (
    JudgeBackend,
    judge_backend_registry,
)

_ASSERTION_LINE_RE = re.compile(r"^\d+\.\s+\(")


class _FakeJudgeBackend:
    def __init__(self, model_name: str) -> None:
        self.model_name = model_name

    async def judge(
        self,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> dict[str, Any]:
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
        return response


def _fake_factory(model_name: str) -> JudgeBackend:
    return _FakeJudgeBackend(model_name)


@pytest.fixture
def fake_judge_backend() -> Iterator[None]:
    prior = dict(judge_backend_registry._factories)
    judge_backend_registry.register("claude", _fake_factory)
    judge_backend_registry.register("gpt", _fake_factory)
    try:
        yield None
    finally:
        judge_backend_registry._factories = prior

@pytest.fixture(autouse=True)
def deterministic_rng() -> Iterator[None]:
    state = random.getstate()
    random.seed(42)
    try:
        yield None
    finally:
        random.setstate(state)
