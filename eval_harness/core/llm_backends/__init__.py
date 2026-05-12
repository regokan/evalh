"""LLM backend registry shared by `llm_judge` and v1 callers (user_simulator,
thinking_does_not_leak).

A backend handles a family of models (e.g. `claude-*`) and exposes a single
`generate` call that takes a prompt and optional JSON schema and returns an
`LlmCall` with text, structured payload, token counts, and cost.

Built-ins register from entry-points (`eval_harness.llm_backends`) at import
time. The legacy `eval_harness.judge_backends` entry-point group is also
loaded for backwards compatibility; each legacy backend is wrapped in a
`JudgeBackendAdapter` that adapts its `judge(prompt, schema, max_tokens)`
dict-returning method to the `LlmBackend.generate` shape.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from eval_harness.core.errors import ConfigError


class LlmCall(BaseModel):
    """One LLM call's outcome — text, parsed structured response, token usage, cost.

    `structured` is populated when the caller supplied a JSON schema; backends
    must return the parsed object there (and the same payload's JSON-encoded form
    in `text` for callers that want raw text).
    """

    text: str = ""
    structured: dict[str, Any] | None = None
    token_input: int = 0
    token_output: int = 0
    token_thinking: int = 0
    cost_usd: float = 0.0
    extra: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LlmBackend(Protocol):
    """A backend dispatches a single LLM call for a model family.

    Implementations validate any required SDK at construction time and raise
    `ConfigError` (with an install hint) if it is missing.
    """

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        schema: dict[str, Any] | None = None,
        cost_limit_usd: float | None = None,
    ) -> LlmCall: ...


class LlmBackendError(Exception):
    """Raised by LLM backends when a call fails in a non-retryable way."""


class LlmBackendParseError(LlmBackendError):
    """The backend returned non-JSON or JSON not matching the requested schema."""


# Legacy aliases retained for third-party plugins that imported these symbols
# from `eval_harness.evaluators._judge_backends`. Identity equality is preserved
# so `isinstance` and `except` chains keep working.
JudgeError = LlmBackendError
JudgeParseError = LlmBackendParseError


@runtime_checkable
class JudgeBackend(Protocol):
    """Legacy backend protocol.

    Kept for type-hints in third-party plugins that pre-date the v1 hoist. New
    backends should implement `LlmBackend`.
    """

    async def judge(
        self,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> dict[str, Any]: ...


LlmBackendFactory = Callable[[], LlmBackend]
_JudgeBackendFactory = Callable[[str], JudgeBackend]


def _wrap_legacy(factory: _JudgeBackendFactory) -> LlmBackendFactory:
    adapter = _JudgeBackendAdapter(factory)
    return lambda: adapter


class _JudgeBackendAdapter:
    """Adapts a legacy `JudgeBackend` factory to the new `LlmBackend` interface.

    The legacy factory takes a model name and returns a per-model instance with
    `judge(prompt, schema, max_tokens) -> dict`. This adapter caches one
    instance per model and exposes them through `generate`.
    """

    def __init__(self, factory: _JudgeBackendFactory) -> None:
        self._factory = factory
        self._instances: dict[str, JudgeBackend] = {}

    def _instance_for(self, model: str) -> JudgeBackend:
        cached = self._instances.get(model)
        if cached is None:
            cached = self._factory(model)
            self._instances[model] = cached
        return cached

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
        backend = self._instance_for(model)
        raw = await backend.judge(prompt, schema or {}, max_tokens)
        usage = raw.pop("_usage", None) if isinstance(raw, dict) else None
        token_input = 0
        token_output = 0
        if isinstance(usage, dict):
            token_input = int(usage.get("input_tokens", 0))
            token_output = int(usage.get("output_tokens", 0))
        structured = raw if isinstance(raw, dict) else None
        return LlmCall(
            text=json.dumps(raw) if structured is not None else "",
            structured=structured,
            token_input=token_input,
            token_output=token_output,
        )


class LlmBackendRegistry:
    """Maps model prefix (e.g. `claude`) -> a factory producing one `LlmBackend`.

    Backends are family singletons: the first `resolve()` for a prefix builds
    the backend and subsequent resolves return the same instance.
    """

    def __init__(self) -> None:
        self._factories: dict[str, LlmBackendFactory] = {}
        self._instances: dict[str, LlmBackend] = {}
        self._entry_points_loaded = False

    def register(self, prefix: str, factory: LlmBackendFactory) -> None:
        self._factories[prefix] = factory
        self._instances.pop(prefix, None)

    def register_legacy(self, prefix: str, factory: _JudgeBackendFactory) -> None:
        self._factories[prefix] = _wrap_legacy(factory)
        self._instances.pop(prefix, None)

    def unregister(self, prefix: str) -> None:
        self._factories.pop(prefix, None)
        self._instances.pop(prefix, None)

    def load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        for ep in entry_points(group="eval_harness.llm_backends"):
            self._factories[ep.name] = ep.load()
        for ep in entry_points(group="eval_harness.judge_backends"):
            # New-style entries win — do not shadow them with the legacy adapter.
            if ep.name in self._factories:
                continue
            self._factories[ep.name] = _wrap_legacy(ep.load())
        self._entry_points_loaded = True

    def resolve(self, model_name: str) -> LlmBackend:
        prefix = model_name.split("-", 1)[0]
        cached = self._instances.get(prefix)
        if cached is not None:
            return cached
        factory = self._factories.get(prefix)
        if factory is None:
            raise ConfigError(
                f"llm_backends: no backend registered for model '{model_name}' "
                f"(prefix '{prefix}'). Known prefixes: {sorted(self._factories)}. "
                f"For 'gpt-*' install `pip install 'eval-harness[openai]'` when "
                f"the OpenAI backend ships."
            )
        instance = factory()
        self._instances[prefix] = instance
        return instance

    def names(self) -> list[str]:
        return sorted(self._factories)


llm_backend_registry = LlmBackendRegistry()


__all__ = [
    "JudgeBackend",
    "JudgeError",
    "JudgeParseError",
    "LlmBackend",
    "LlmBackendError",
    "LlmBackendParseError",
    "LlmBackendRegistry",
    "LlmCall",
    "llm_backend_registry",
]
