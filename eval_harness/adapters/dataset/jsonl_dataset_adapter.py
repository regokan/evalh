from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

from pydantic import ValidationError

from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import EvalCase


class JsonlDatasetAdapter:
    """One JSON object per line. Each line is an EvalCase.

    Parallel to YamlDatasetAdapter — same Protocol, lighter format. Useful when
    cases come from notebook output or production telemetry pipelines that
    already emit JSONL.
    """

    embed_full_trace: ClassVar[bool] = False

    def __init__(self, path: str | Path, **kwargs: Any) -> None:
        self.path = Path(path)
        self._extra = kwargs

    async def load_cases(self) -> list[EvalCase]:
        try:
            raw = self.path.read_text()
        except OSError as e:
            raise AdapterError(f"Cannot read dataset file {self.path}: {e}") from e

        cases: list[EvalCase] = []
        seen_ids: set[str] = set()
        for lineno, line in enumerate(raw.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise ConfigError(
                    f"{self.path}:{lineno}: malformed JSON: {e.msg} "
                    f"(col {e.colno})"
                ) from e
            if not isinstance(obj, dict):
                raise ConfigError(
                    f"{self.path}:{lineno}: line must be a JSON object, got "
                    f"{type(obj).__name__}"
                )
            try:
                case = EvalCase.model_validate(obj)
            except ValidationError as e:
                raise ConfigError(
                    f"{self.path}:{lineno}: invalid EvalCase: {e}"
                ) from e
            if case.id in seen_ids:
                raise ConfigError(
                    f"{self.path}:{lineno}: duplicate case id '{case.id}'"
                )
            seen_ids.add(case.id)
            cases.append(case)

        return cases
