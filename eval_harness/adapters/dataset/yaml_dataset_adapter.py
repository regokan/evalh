from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import EvalCase, ExpectedBehavior


class YamlDatasetAdapter:
    def __init__(self, path: str | Path, **kwargs: Any) -> None:
        self.path = Path(path)
        self._extra = kwargs

    async def load_cases(self) -> list[EvalCase]:
        try:
            raw = self.path.read_text()
        except OSError as e:
            raise AdapterError(f"Cannot read dataset file {self.path}: {e}") from e

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise ConfigError(f"YAML parse error in {self.path}: {e}") from e

        if data is None:
            raise ConfigError(f"Dataset file {self.path} is empty")
        if not isinstance(data, dict):
            raise ConfigError(
                f"Top-level of {self.path} must be a mapping, got {type(data).__name__}"
            )

        schema_version = data.get("schema_version")
        if schema_version is None:
            raise ConfigError(f"{self.path}: missing required 'schema_version'")
        if not isinstance(schema_version, str):
            raise ConfigError(
                f"{self.path}: 'schema_version' must be a string, got "
                f"{type(schema_version).__name__}"
            )
        major = schema_version.split(".", 1)[0]
        if major != "1":
            raise ConfigError(
                f"{self.path}: unsupported schema_version '{schema_version}'; "
                f"this version of eval-harness reads 1.x files only"
            )

        raw_cases = data.get("cases")
        if not isinstance(raw_cases, list):
            raise ConfigError(
                f"{self.path}: 'cases' must be a list, got {type(raw_cases).__name__}"
            )

        cases: list[EvalCase] = []
        seen_ids: set[str] = set()
        for i, raw_case in enumerate(raw_cases):
            if not isinstance(raw_case, dict):
                raise ConfigError(
                    f"{self.path}: cases[{i}] must be a mapping, got "
                    f"{type(raw_case).__name__}"
                )
            case = self._parse_case(raw_case, i)
            if case.id in seen_ids:
                raise ConfigError(
                    f"{self.path}: duplicate case id '{case.id}' (cases[{i}])"
                )
            seen_ids.add(case.id)
            cases.append(case)

        return cases

    def _parse_case(self, raw: dict[str, Any], index: int) -> EvalCase:
        expected_raw = raw.get("expected")
        if expected_raw is None:
            expected = ExpectedBehavior()
        elif isinstance(expected_raw, dict):
            try:
                expected = ExpectedBehavior.model_validate(expected_raw)
            except ValidationError as e:
                raise ConfigError(
                    f"{self.path}: cases[{index}].expected invalid: {e}"
                ) from e
        else:
            raise ConfigError(
                f"{self.path}: cases[{index}].expected must be a mapping, got "
                f"{type(expected_raw).__name__}"
            )

        payload = {
            "id": raw.get("id"),
            "input": raw.get("input"),
            "metadata": raw.get("metadata", {}),
            "expected": expected,
        }
        if "schema_version" in raw:
            payload["schema_version"] = raw["schema_version"]

        try:
            return EvalCase.model_validate(payload)
        except ValidationError as e:
            raise ConfigError(f"{self.path}: cases[{index}] invalid: {e}") from e
