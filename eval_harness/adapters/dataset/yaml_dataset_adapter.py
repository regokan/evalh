from __future__ import annotations

from typing import Any

from eval_harness.core.models import EvalCase


class YamlDatasetAdapter:
    def __init__(self, **config: Any) -> None:
        self._config = config

    async def load_cases(self) -> list[EvalCase]:
        raise NotImplementedError("YamlDatasetAdapter.load_cases lands in ev-t62")
