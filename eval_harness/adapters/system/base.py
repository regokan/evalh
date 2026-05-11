from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self, runtime_checkable

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.models import EvalCase, RunVariant, Trace


@runtime_checkable
class SystemAdapter(Protocol):
    name: str

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace: ...
