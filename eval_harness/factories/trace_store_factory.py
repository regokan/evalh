from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eval_harness.core.errors import ConfigError
from eval_harness.core.registry import Registry

if TYPE_CHECKING:
    from eval_harness.adapters.trace.base import TraceStore

_ENTRY_POINT_GROUP = "eval_harness.trace_stores"


class TraceStoreFactory:
    def __init__(self) -> None:
        self.registry: Registry[Any] = Registry("trace_store")
        self._entry_points_loaded = False

    def register(self, name: str, cls: type[Any]) -> None:
        self.registry.register(name, cls)

    def load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        self.registry.load_entry_points(_ENTRY_POINT_GROUP)
        self._entry_points_loaded = True

    def build(self, config: dict[str, Any]) -> TraceStore:
        type_ = config.get("type")
        if not type_:
            raise ConfigError("output (trace store) config missing 'type'")
        cls = self.registry.get(type_)
        kwargs = {k: v for k, v in config.items() if k != "type"}
        instance: TraceStore = cls(**kwargs)
        return instance
