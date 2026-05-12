"""Judge backend registry.

Internal seam for `llm_judge`. The evaluator looks up a backend by model prefix
(everything before the first `-`); tests inject fakes by registering a factory
here instead of mocking `anthropic.AsyncAnthropic`.

Built-ins register from entry-points (`eval_harness.judge_backends`) at import
time.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Protocol, runtime_checkable

from eval_harness.core.errors import ConfigError


@runtime_checkable
class JudgeBackend(Protocol):
    """A judge backend turns a prompt into a parsed JSON response.

    Implementations validate any required SDK at construction time and raise
    `ConfigError` (with an install hint) if it's missing.
    """

    async def judge(
        self,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> dict[str, Any]: ...


class JudgeError(Exception):
    """Raised by judge backends when a call fails in a non-retryable way."""


class JudgeParseError(JudgeError):
    """The judge returned non-JSON or JSON not matching the requested schema."""


JudgeBackendFactory = Any  # Callable[[str], JudgeBackend] — Any for mypy entry-point loading


class JudgeBackendRegistry:
    """Maps model prefix (e.g. `claude`) -> factory callable that builds a backend.

    The factory takes the full model name (e.g. `claude-4-7`) so the backend can
    record it and pass it to the underlying SDK.
    """

    def __init__(self) -> None:
        self._factories: dict[str, JudgeBackendFactory] = {}
        self._entry_points_loaded = False

    def register(self, prefix: str, factory: JudgeBackendFactory) -> None:
        self._factories[prefix] = factory

    def unregister(self, prefix: str) -> None:
        self._factories.pop(prefix, None)

    def load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        for ep in entry_points(group="eval_harness.judge_backends"):
            self._factories[ep.name] = ep.load()
        self._entry_points_loaded = True

    def resolve(self, model_name: str) -> JudgeBackend:
        prefix = model_name.split("-", 1)[0]
        factory = self._factories.get(prefix)
        if factory is None:
            raise ConfigError(
                f"llm_judge: no judge backend registered for model '{model_name}' "
                f"(prefix '{prefix}'). Known prefixes: {sorted(self._factories)}. "
                f"For 'gpt-*' install `pip install 'eval-harness[openai]'` when "
                f"the OpenAI backend ships."
            )
        return factory(model_name)  # type: ignore[no-any-return]

    def names(self) -> list[str]:
        return sorted(self._factories)


judge_backend_registry = JudgeBackendRegistry()


__all__ = [
    "JudgeBackend",
    "JudgeBackendRegistry",
    "JudgeError",
    "JudgeParseError",
    "judge_backend_registry",
]
