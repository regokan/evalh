"""OpenAI embedder backend. Optional install — `pip install 'eval-harness[openai]'`.

Registered as `openai-text-embedding-3-small` so the model is visible in
`evaluators[].config.embedder_name` instead of being a hidden constructor
default. The model name doubles as the registry key — same pattern as the
sentence-transformers backend.
"""

from __future__ import annotations

from typing import Any

from eval_harness.core.errors import ConfigError

_MODEL = "text-embedding-3-small"


class OpenAIEmbedder:
    """Wraps `openai.AsyncOpenAI.embeddings.create` for the EmbedderBackend
    protocol."""

    def __init__(self) -> None:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise ConfigError(
                "OpenAIEmbedder requires the openai SDK. Install with: "
                "pip install 'eval-harness[openai]'"
            ) from e
        self._client = AsyncOpenAI()
        self._model = _MODEL

    async def embed(self, text: str) -> list[float]:
        resp: Any = await self._client.embeddings.create(model=self._model, input=text)
        # OpenAI returns a CreateEmbeddingResponse; .data is a list of one
        # Embedding for a single-string input.
        return list(resp.data[0].embedding)
