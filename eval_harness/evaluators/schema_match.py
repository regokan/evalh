from __future__ import annotations

from typing import Any, ClassVar

import jsonschema
from jsonpath_ng import parse as jsonpath_parse

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.base import Evaluator

_DEFAULT_FIELD = "output.structured"
_SENTINEL = object()


class SchemaMatchEvaluator(Evaluator):
    type: ClassVar[str] = "schema_match"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        schema = config.get("schema")
        if not isinstance(schema, dict):
            raise ConfigError("schema_match: 'schema' (dict) is required")
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
        except jsonschema.SchemaError as e:
            raise ConfigError(f"schema_match: invalid JSON schema: {e.message}") from e
        if "field" in config and not isinstance(config["field"], str):
            raise ConfigError("schema_match: 'field' must be a string JSONPath")

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()
        schema: dict[str, Any] = self._config["schema"]
        field: str = self._config.get("field", _DEFAULT_FIELD)

        value = _jsonpath_first(trace.model_dump(mode="python"), field, _SENTINEL)
        if value is _SENTINEL:
            return _result(
                self,
                case,
                trace,
                started,
                passed=False,
                reason=f"field '{field}' not present in trace",
                detail={"field": field, "schema_id": schema.get("$id"), "errors": []},
            )

        validator = jsonschema.Draft202012Validator(schema)
        errors = sorted(validator.iter_errors(value), key=lambda e: e.path)
        passed = not errors

        return _result(
            self,
            case,
            trace,
            started,
            passed=passed,
            reason=("schema matched" if passed else f"{len(errors)} schema error(s)"),
            detail={
                "field": field,
                "schema_id": schema.get("$id"),
                "errors": [e.message for e in errors],
            },
        )


def _result(
    evaluator: SchemaMatchEvaluator,
    case: EvalCase,
    trace: Trace,
    started: Any,
    *,
    passed: bool,
    reason: str,
    detail: dict[str, Any],
) -> EvaluationResult:
    finished = utc_now()
    return EvaluationResult(
        run_id=trace.run_id,
        case_id=case.id,
        variant_name=trace.variant_name,
        evaluator=evaluator.name,
        evaluator_type=evaluator.type,
        passed=passed,
        reason=reason,
        detail=detail,
        started_at=started,
        finished_at=finished,
        latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
    )


def _jsonpath_first(data: Any, path: str, default: Any) -> Any:
    matches = jsonpath_parse(path).find(data)
    if not matches:
        return default
    return matches[0].value
