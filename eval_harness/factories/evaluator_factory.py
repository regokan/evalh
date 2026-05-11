from __future__ import annotations

from typing import TYPE_CHECKING, Any

from eval_harness.core.registry import Registry

if TYPE_CHECKING:
    from eval_harness.core.config import EvaluatorConfig
    from eval_harness.evaluators.base import Evaluator

_ENTRY_POINT_GROUP = "eval_harness.evaluators"


class EvaluatorFactory:
    def __init__(self) -> None:
        self.registry: Registry[Any] = Registry("evaluator")
        self._entry_points_loaded = False

    def register(self, name: str, cls: type[Any]) -> None:
        self.registry.register(name, cls)

    def load_entry_points(self) -> None:
        if self._entry_points_loaded:
            return
        self.registry.load_entry_points(_ENTRY_POINT_GROUP)
        self._entry_points_loaded = True

    def build(self, config: EvaluatorConfig) -> Evaluator:
        cls = self.registry.get(config.type)
        cls.validate_config(config.config)
        instance: Evaluator = cls(name=config.name, **config.config)
        return instance
