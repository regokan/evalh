"""ObjectStorage abstraction + fsspec-backed implementation.

Public surface:

- ``ObjectStorage``: Protocol implemented by every backend.
- ``FsspecObjectStorage``: catch-all implementation; handles file://, s3://,
  gs://, az://, memory://, … because fsspec already abstracts them.
- ``ObjectStorageRegistry``: entry-point-loaded registry under
  ``eval_harness.object_storages``. Built-in registration: ``fsspec``.
- ``get_default_object_storage(run_dir)``: build a local-default storage
  rooted at ``runs/<run_id>/artifacts/`` so single-machine `evalh run`
  preserves existing semantics with no opt-in.
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from threading import Lock
from typing import Any

from eval_harness.core.errors import ConfigError
from eval_harness.core.object_storage.base import ObjectStorage
from eval_harness.core.object_storage.fsspec_storage import FsspecObjectStorage

_ENTRY_POINT_GROUP = "eval_harness.object_storages"


class ObjectStorageRegistry:
    """Maps a short name (``fsspec``) to a factory callable that builds an
    ObjectStorage from a config dict.

    Mirrors the other family registries — built-ins register via entry-points
    at first ``load_entry_points()``; tests and plugins can register
    additional backends programmatically.
    """

    def __init__(self) -> None:
        self._factories: dict[str, type[Any]] = {}
        self._entry_points_loaded = False
        self._lock = Lock()

    def register(self, name: str, factory: type[Any]) -> None:
        with self._lock:
            self._factories[name] = factory

    def load_entry_points(self) -> None:
        with self._lock:
            if self._entry_points_loaded:
                return
            for ep in entry_points(group=_ENTRY_POINT_GROUP):
                self._factories[ep.name] = ep.load()
            self._entry_points_loaded = True

    def build(self, config: dict[str, Any]) -> ObjectStorage:
        type_ = config.get("type")
        if not type_:
            raise ConfigError("object_storage config missing 'type'")
        factory = self._factories.get(type_)
        if factory is None:
            raise ConfigError(
                f"Unknown object_storage '{type_}'. Registered: "
                f"{sorted(self._factories)}"
            )
        kwargs = {k: v for k, v in config.items() if k != "type"}
        instance: ObjectStorage = factory(**kwargs)
        return instance

    def names(self) -> list[str]:
        return sorted(self._factories)


object_storage_registry = ObjectStorageRegistry()


def get_default_object_storage(run_dir: Path) -> ObjectStorage:
    """Build a local-default ObjectStorage rooted under ``run_dir``.

    Single-machine `evalh run` doesn't have to opt in to anything: the
    workspace adapter pulls this by default so existing tests that read
    `runs/<run_id>/artifacts/<case>/<variant>/artifact.json` keep working.
    """
    url = run_dir.resolve().as_uri()
    return FsspecObjectStorage(url=url)


__all__ = [
    "FsspecObjectStorage",
    "ObjectStorage",
    "ObjectStorageRegistry",
    "get_default_object_storage",
    "object_storage_registry",
]
