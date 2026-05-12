from __future__ import annotations

from typing import ClassVar, Protocol, runtime_checkable

from eval_harness.core.models import EvalCase


@runtime_checkable
class DatasetAdapter(Protocol):
    """A dataset adapter loads `EvalCase`s from somewhere.

    Implementations that pull from observability platforms (langfuse, phoenix,
    arize, fixture) support a class-level ``embed_full_trace`` Protocol flag
    that mirrors the YAML config key of the same name:

    - ``False`` (default): emit cases with ``case.input`` only — the user
      request as captured upstream. Suitable for fresh SystemAdapters
      (http/python_function/cli) that re-run the system.
    - ``True``: emit cases with ``case._embedded_trace`` populated. Suitable
      for the ``replay`` SystemAdapter (online evaluation: score what already
      happened, no system call).

    Adapters that have no notion of an upstream trace (yaml, jsonl) keep the
    flag as ``False`` and ignore the YAML key.
    """

    embed_full_trace: ClassVar[bool]

    async def load_cases(self) -> list[EvalCase]: ...
