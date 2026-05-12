from __future__ import annotations

from typing import Any, ClassVar

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

_SENTINEL = object()


class ExactMatchEvaluator(Evaluator):
    type: ClassVar[str] = "exact_match"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        if "field" not in config or not isinstance(config["field"], str):
            raise ConfigError("exact_match: 'field' (str JSONPath) is required")
        if "expected" not in config and "expected_from" not in config:
            raise ConfigError(
                "exact_match: one of 'expected' or 'expected_from' is required"
            )
        if "expected_from" in config and not isinstance(config["expected_from"], str):
            raise ConfigError("exact_match: 'expected_from' must be a string JSONPath")

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()

        cfg = self._config
        field: str = cfg["field"]

        actual = _jsonpath_first(trace.model_dump(mode="python"), field, _SENTINEL)

        if "expected" in cfg:
            expected: Any = cfg["expected"]
            expected_source = "config.expected"
        else:
            path = cfg["expected_from"]
            expected = _jsonpath_first(case.expected.facts, path, _SENTINEL)
            expected_source = f"case.expected.facts via '{path}'"

        if actual is _SENTINEL:
            passed = False
            reason = f"field '{field}' not present in trace"
            actual_for_detail: Any = None
        elif expected is _SENTINEL:
            passed = False
            reason = f"expected value not found at {expected_source}"
            actual_for_detail = actual
        else:
            passed = actual == expected
            reason = "exact match" if passed else "values differ"
            actual_for_detail = actual

        finished = utc_now()
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=passed,
            reason=reason,
            detail={
                "field": field,
                "actual": actual_for_detail,
                "expected": expected if expected is not _SENTINEL else None,
                "expected_source": expected_source,
            },
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )


def _jsonpath_first(data: Any, path: str, default: Any) -> Any:
    matches = jsonpath_parse(path).find(data)
    if not matches:
        return default
    return matches[0].value
