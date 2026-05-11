from __future__ import annotations

from eval_harness.adapters.dataset.base import DatasetAdapter
from eval_harness.factories import dataset_adapter_factory

dataset_adapter_factory.load_entry_points()

__all__ = ["DatasetAdapter"]
