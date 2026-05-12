"""Embedder backend registry — pluggable backend for `semantic_similarity`.

Shaped exactly like `eval_harness.core.llm_backends`: third-party plugins
register an `EmbedderBackend` factory via the `eval_harness.embedders`
entry-point group. NO default backend ships — installing eval-harness
without `[openai]` or `[embeddings_local]` and trying to use
`semantic_similarity` raises a `ConfigError` at plan time with an
install hint pointing at the right extra.
"""

from __future__ import annotations

from collections.abc import Callable
from importlib.metadata import entry_points
from typing import Protocol, runtime_checkable

from eval_harness.core.errors import ConfigError


@runtime_checkable
class EmbedderBackend(Protocol):
    """A backend turns a single text into a fixed-length embedding vector.

    Implementations validate any required SDK at construction time and
    raise `ConfigError` (with an install hint) if it is missing.
    """

    async def embed(self, text: str) -> list[float]: ...


EmbedderFactory = Callable[[], EmbedderBackend]


class EmbedderRegistry:
    """Maps backend name -> a factory producing one `EmbedderBackend`.

    Backends are singletons per name: the first `resolve()` for a name
    builds the backend and subsequent resolves return the same instance —
    so model weights / HTTP clients load once per process.
    """

    def __init__(self) -> None:
        self._factories: dict[str, EmbedderFactory] = {}
        self._instances: dict[str, EmbedderBackend] = {}
        self._entry_points_loaded = False

    def register(self, name: str, factory: EmbedderFactory) -> None:
        self._factories[name] = factory
        self._instances.pop(name, None)

    def unregister(self, name: str) -> None:
        self._factories.pop(name, None)
        self._instances.pop(name, None)

    def load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        for ep in entry_points(group="eval_harness.embedders"):
            self._factories[ep.name] = ep.load()
        self._entry_points_loaded = True

    def resolve(self, name: str) -> EmbedderBackend:
        cached = self._instances.get(name)
        if cached is not None:
            return cached
        factory = self._factories.get(name)
        if factory is None:
            known = sorted(self._factories)
            known_blurb = (
                str(known)
                if known
                else "(none installed — pip install 'eval-harness[openai]' "
                "or 'eval-harness[embeddings_local]')"
            )
            raise ConfigError(
                f"semantic_similarity: no embedder registered as {name!r}. "
                f"Known: {known_blurb}. Embedders register via the "
                f"`eval_harness.embedders` entry-point group."
            )
        instance = factory()
        self._instances[name] = instance
        return instance

    def names(self) -> list[str]:
        return sorted(self._factories)


embedder_registry = EmbedderRegistry()


__all__ = [
    "EmbedderBackend",
    "EmbedderRegistry",
    "embedder_registry",
]
