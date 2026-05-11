from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar

from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
)


class Evaluator(ABC):
    type: ClassVar[str] = ""

    def __init__(self, name: str, **config: Any) -> None:
        self.name = name
        self._config = config

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        return None

    @abstractmethod
    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult: ...
