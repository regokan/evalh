"""Local sentence-transformers embedder. Optional install —
`pip install 'eval-harness[embeddings_local]'`.

Default model is `sentence-transformers/all-MiniLM-L6-v2` (~80 MB on disk)
which doubles as the registry key. The SDK is sync, so `embed` runs in a
thread.
"""

from __future__ import annotations

import asyncio
from typing import Any

from eval_harness.core.errors import ConfigError

_MODEL = "sentence-transformers/all-MiniLM-L6-v2"


class SentenceTransformersEmbedder:
    """Wraps `sentence_transformers.SentenceTransformer.encode` for the
    EmbedderBackend protocol."""

    def __init__(self) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as e:
            raise ConfigError(
                "SentenceTransformersEmbedder requires sentence-transformers. "
                "Install with: pip install 'eval-harness[embeddings_local]'"
            ) from e
        # Model load is non-trivial (downloads on first use); doing it in
        # __init__ keeps cost out of the per-call path.
        self._model = SentenceTransformer(_MODEL)

    async def embed(self, text: str) -> list[float]:
        vector: Any = await asyncio.to_thread(self._encode, text)
        return list(vector)

    def _encode(self, text: str) -> Any:
        # `encode` returns numpy.ndarray; .tolist() materialises a Python list.
        return self._model.encode(text, convert_to_numpy=True).tolist()
