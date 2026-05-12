"""Runner-shared LLM backend registry.

Originally lived under ``eval_harness/evaluators/_judge_backends/`` and was
only used by ``llm_judge``. v1 hoists it into ``eval_harness.core`` so the
upcoming multi-turn ``user_simulator`` SystemAdapter and the
``thinking_does_not_leak`` evaluator can share one prompt+schema+cost path.

The Protocol is ``LlmBackend``, the return type is ``LlmCall``, and backends
register via the ``eval_harness.llm_backends`` entry-point group.

Backwards compatibility: the legacy ``eval_harness.judge_backends`` group is
still loaded — old ``JudgeBackend`` factories are wrapped on the fly by
``_JudgeBackendLlmAdapter`` so third-party plugins keep working for one
milestone. New code should target ``eval_harness.llm_backends``.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from eval_harness.core.errors import ConfigError


class LlmCall(BaseModel):
    """The shape every ``LlmBackend.generate`` resolves to."""

    text: str
    structured: dict[str, Any] | None = None
    token_input: int | None = None
    token_output: int | None = None
    token_thinking: int | None = None
    cost_usd: float | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class LlmBackend(Protocol):
    """One model family's call surface.

    Implementations validate any required SDK at construction time and raise
    :class:`ConfigError` with an install hint if it's missing.
    """

    model: str

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        system: str | None = None,
        schema: dict[str, Any] | None = None,
        cost_limit_usd: float | None = None,
    ) -> LlmCall: ...


class LlmCallCostLimitError(Exception):
    """Raised by a backend when the estimated cost exceeds ``cost_limit_usd``.

    Carries the estimate so callers can record it in their result detail.
    """

    def __init__(self, estimated_usd: float, limit_usd: float) -> None:
        super().__init__(
            f"estimated cost ${estimated_usd:.4f} exceeds cost_limit_usd "
            f"${limit_usd:.4f}"
        )
        self.estimated_usd = estimated_usd
        self.limit_usd = limit_usd


class LlmParseError(Exception):
    """The backend returned non-JSON or JSON not matching the requested schema."""


LlmBackendFactory = Any  # Callable[[str], LlmBackend]


class LlmBackendRegistry:
    """Maps a model-name prefix (e.g. ``claude``, ``gpt``) to a factory."""

    def __init__(self) -> None:
        self._factories: dict[str, LlmBackendFactory] = {}
        self._entry_points_loaded = False

    def register(self, prefix: str, factory: LlmBackendFactory) -> None:
        self._factories[prefix] = factory

    def unregister(self, prefix: str) -> None:
        self._factories.pop(prefix, None)

    def load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return

        # Native v1 group wins. Each entry should yield a class compatible with
        # the LlmBackend Protocol.
        for ep in entry_points(group="eval_harness.llm_backends"):
            self._factories[ep.name] = ep.load()

        # Legacy v0.x group — adapt JudgeBackend factories to the new
        # interface so existing third-party plugins keep working. New entries
        # never override v1 ones; the v1 group is authoritative.
        for ep in entry_points(group="eval_harness.judge_backends"):
            if ep.name in self._factories:
                continue
            judge_factory = ep.load()
            self._factories[ep.name] = _make_judge_adapter_factory(judge_factory)

        self._entry_points_loaded = True

    def resolve(self, model_name: str) -> LlmBackend:
        prefix = model_name.split("-", 1)[0]
        factory = self._factories.get(prefix)
        if factory is None:
            raise ConfigError(
                f"no LlmBackend registered for model '{model_name}' "
                f"(prefix '{prefix}'). Known prefixes: {sorted(self._factories)}. "
                f"For 'gpt-*' install `pip install 'eval-harness[openai]'` "
                f"when the OpenAI backend ships."
            )
        return factory(model_name)  # type: ignore[no-any-return]

    def names(self) -> list[str]:
        return sorted(self._factories)


llm_backend_registry = LlmBackendRegistry()


def _make_judge_adapter_factory(judge_factory: Any) -> LlmBackendFactory:
    """Wrap a legacy ``JudgeBackend`` factory into an LlmBackend factory."""

    def _build(model_name: str) -> LlmBackend:
        judge = judge_factory(model_name)
        return _JudgeBackendLlmAdapter(judge=judge, model=model_name)

    return _build


class _JudgeBackendLlmAdapter:
    """Adapt a v0.x ``JudgeBackend`` (``.judge(prompt, schema, max_tokens)``) to
    the v1 ``LlmBackend`` Protocol (``.generate(prompt, *, ...)``)."""

    def __init__(self, *, judge: Any, model: str) -> None:
        self._judge = judge
        self.model = model

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        system: str | None = None,  # noqa: ARG002 — legacy judges don't take system
        schema: dict[str, Any] | None = None,
        cost_limit_usd: float | None = None,  # noqa: ARG002 — legacy judges no pre-check
    ) -> LlmCall:
        import json as _json

        data = await self._judge.judge(prompt, schema or {}, max_tokens)
        usage = data.pop("_usage", None) if isinstance(data, dict) else None
        token_input = (
            int(usage.get("input_tokens", 0))
            if isinstance(usage, dict)
            else None
        )
        token_output = (
            int(usage.get("output_tokens", 0))
            if isinstance(usage, dict)
            else None
        )
        return LlmCall(
            text=_json.dumps(data) if isinstance(data, dict) else str(data),
            structured=data if isinstance(data, dict) else None,
            token_input=token_input,
            token_output=token_output,
            cost_usd=None,
        )


__all__ = [
    "LlmBackend",
    "LlmBackendRegistry",
    "LlmCall",
    "LlmCallCostLimitError",
    "LlmParseError",
    "llm_backend_registry",
]
