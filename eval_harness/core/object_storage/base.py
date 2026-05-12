"""ObjectStorage Protocol — bytes-mover abstraction for artifacts.

The runtime ships FilesystemArtifacts (and, eventually, anything else that's
bytes) through this Protocol so v2's distributed executors can write to a
central place (s3://, gs://, a shared file://, …) while local dev keeps the
existing ``runs/<run_id>/`` layout. fsspec backs the default implementation
in ``fsspec_storage.py``; consumers only need this Protocol.

All methods are async — the production implementation wraps sync fsspec
calls with ``asyncio.to_thread`` so the runner's event loop doesn't block.
"""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, runtime_checkable


@runtime_checkable
class ObjectStorage(Protocol):
    """Minimal bytes-mover surface.

    ``put`` returns the canonical URL of the stored object so consumers can
    record it on the trace/artifact and downstream tooling can fetch it.
    """

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def put(self, key: str, data: bytes) -> str:
        """Write ``data`` at ``key``. Return the storage URL."""
        ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...

    async def list_prefix(self, prefix: str) -> list[str]: ...
