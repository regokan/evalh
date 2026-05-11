from __future__ import annotations

from eval_harness.adapters.system.base import SystemAdapter
from eval_harness.factories import system_adapter_factory

system_adapter_factory.load_entry_points()

__all__ = ["SystemAdapter"]
