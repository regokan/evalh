from __future__ import annotations

from types import TracebackType
from typing import Any, Self

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.models import EvalCase, RunVariant, Trace


class PythonFunctionAdapter:
    name: str

    def __init__(self, name: str = "python_function", **config: Any) -> None:
        self.name = name
        self._config = config

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        raise NotImplementedError("PythonFunctionAdapter.run lands in ev-1t2")
