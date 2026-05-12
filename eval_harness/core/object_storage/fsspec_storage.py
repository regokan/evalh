"""fsspec-backed ObjectStorage.

One class handles every backend fsspec knows about — ``file://``, ``s3://``,
``gs://``, ``az://``, ``memory://``, … — because fsspec already abstracts
them. Adding a new cloud backend is an extras install (``s3fs`` / ``gcsfs``
/ ``adlfs``), not new code here.

Determinism / async: fsspec's sync APIs are wrapped with
``asyncio.to_thread``. The synchronous calls are tiny per operation so the
thread overhead is acceptable; alternative is fsspec's experimental async
filesystems, which not every backend supports yet.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse

from eval_harness.core.errors import ConfigError


class FsspecObjectStorage:
    """Single fsspec-backed ObjectStorage. Configured by one URL.

    ``url``: the storage root, e.g. ``file:///abs/path/to/runs``,
    ``s3://bucket/prefix``, ``gs://bucket/prefix``, ``memory://test``.
    Keys are appended to this root with ``/`` joins.

    ``credentials``: optional dict forwarded to ``fsspec.filesystem(...)``.
    s3fs reads its own env vars by default; this is for callers that want
    to inject creds explicitly.
    """

    def __init__(
        self,
        *,
        url: str | None = None,
        credentials: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        if not url:
            raise ConfigError(
                "FsspecObjectStorage: 'url' is required (e.g. 'file:///path' "
                "or 's3://bucket/prefix')"
            )
        try:
            import fsspec
        except ImportError as e:  # pragma: no cover — fsspec is in core deps
            raise ConfigError(
                "FsspecObjectStorage requires fsspec. Install with: "
                "pip install fsspec"
            ) from e
        parsed = urlparse(url)
        self.url = url.rstrip("/")
        self.scheme = (parsed.scheme or "file").lower()
        self._credentials = dict(credentials or {})
        # `fsspec.filesystem` returns the protocol handler. The handler
        # is reusable; we keep one per FsspecObjectStorage so backends
        # like s3fs share their connection pool across `put`/`get` calls.
        self._fs: Any = fsspec.filesystem(self.scheme, **self._credentials)
        self._opened = False

    async def __aenter__(self) -> Self:
        self._opened = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._opened = False
        # fsspec handlers don't expose a portable shutdown; leave it to GC.
        # (Specific protocols like s3fs lazily close on interpreter exit.)
        return None

    def _join(self, key: str) -> str:
        # urljoin doesn't quite do what we need for protocol-prefixed URLs;
        # a path-style join with a single separator is enough.
        return f"{self.url}/{key.lstrip('/')}"

    async def put(self, key: str, data: bytes) -> str:
        path = self._join(key)
        await asyncio.to_thread(self._put_sync, path, data)
        return path

    def _put_sync(self, path: str, data: bytes) -> None:
        # Ensure parent dirs exist for filesystem-like backends; cloud
        # backends ignore the call.
        parent = path.rsplit("/", 1)[0]
        # Some fsspec backends (memory://, cloud) don't expose makedirs.
        with contextlib.suppress(NotImplementedError, AttributeError):
            self._fs.makedirs(parent, exist_ok=True)
        with self._fs.open(path, "wb") as f:
            f.write(data)

    async def get(self, key: str) -> bytes:
        path = self._join(key)
        return await asyncio.to_thread(self._get_sync, path)

    def _get_sync(self, path: str) -> bytes:
        with self._fs.open(path, "rb") as f:
            payload = f.read()
        if isinstance(payload, str):  # pragma: no cover — some FS return str
            return payload.encode()
        out: bytes = payload
        return out

    async def exists(self, key: str) -> bool:
        path = self._join(key)
        return await asyncio.to_thread(self._exists_sync, path)

    def _exists_sync(self, path: str) -> bool:
        result: bool = bool(self._fs.exists(path))
        return result

    async def list_prefix(self, prefix: str) -> list[str]:
        path = self._join(prefix)
        return await asyncio.to_thread(self._list_sync, path)

    def _list_sync(self, path: str) -> list[str]:
        try:
            raw = self._fs.ls(path, detail=False)
        except FileNotFoundError:
            return []
        out: list[str] = []
        for entry in raw:
            if isinstance(entry, dict):
                entry = entry.get("name", "")
            if not entry:
                continue
            text = str(entry)
            # Some fsspec backends (memory://) strip the protocol from `ls`
            # results; re-prefix so callers always get round-trippable URLs.
            if "://" not in text:
                text = f"{self.scheme}://{text.lstrip('/')}"
            out.append(text)
        return sorted(out)
