from __future__ import annotations

from importlib.metadata import entry_points
from typing import Generic, TypeVar, cast

from eval_harness.core.errors import ConfigError

T = TypeVar("T")


class Registry(Generic[T]):
    def __init__(self, kind: str) -> None:
        self._kind = kind
        self._items: dict[str, type[T]] = {}

    def register(self, name: str, cls: type[T]) -> None:
        self._items[name] = cls

    def get(self, name: str) -> type[T]:
        if name not in self._items:
            raise ConfigError(
                f"Unknown {self._kind} '{name}'. Registered: {sorted(self._items)}"
            )
        return self._items[name]

    def load_entry_points(self, group_name: str) -> None:
        for ep in entry_points(group=group_name):
            self._items[ep.name] = cast("type[T]", ep.load())

    def names(self) -> list[str]:
        return sorted(self._items)
