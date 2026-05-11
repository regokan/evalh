from __future__ import annotations

from typing import Protocol, runtime_checkable

from eval_harness.core.models import EvalCase


@runtime_checkable
class DatasetAdapter(Protocol):
    async def load_cases(self) -> list[EvalCase]: ...
