from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eval_harness.core.errors import ConfigError
from eval_harness.core.registry import Registry

if TYPE_CHECKING:
    from eval_harness.adapters.system.base import SystemAdapter
    from eval_harness.core.models import RunVariant

_ENTRY_POINT_GROUP = "eval_harness.system_adapters"


class SystemAdapterFactory:
    def __init__(self) -> None:
        self.registry: Registry[Any] = Registry("system_adapter")
        self._entry_points_loaded = False

    def register(self, name: str, cls: type[Any]) -> None:
        self.registry.register(name, cls)

    def load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        self.registry.load_entry_points(_ENTRY_POINT_GROUP)
        self._entry_points_loaded = True

    def build(self, variant: RunVariant) -> SystemAdapter:
        if not variant.adapter:
            raise ConfigError("system variant missing 'adapter'")
        cls = self.registry.get(variant.adapter)
        instance: SystemAdapter = cls(name=variant.name, **variant.config)
        return instance
