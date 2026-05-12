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

_DEFAULT_FIELD = "output.final_answer"


class ContainsTextEvaluator(Evaluator):
    type: ClassVar[str] = "contains_text"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        for key in ("all_of", "any_of", "none_of"):
            val = config.get(key)
            if val is not None and not (
                isinstance(val, list) and all(isinstance(s, str) for s in val)
            ):
                raise ConfigError(
                    f"contains_text: '{key}' must be a list[str], got {val!r}"
                )
        if "case_sensitive" in config and not isinstance(config["case_sensitive"], bool):
            raise ConfigError("contains_text: 'case_sensitive' must be bool")
        if "field" in config and not isinstance(config["field"], str):
            raise ConfigError("contains_text: 'field' must be a string JSONPath")

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()

        cfg = self._config
        field = cfg.get("field", _DEFAULT_FIELD)
        case_sensitive = bool(cfg.get("case_sensitive", False))

        all_of = cfg.get("all_of")
        if all_of is None:
            all_of = list(case.expected.answer_should_include)
        none_of = cfg.get("none_of")
        if none_of is None:
            none_of = list(case.expected.answer_should_not_include)
        any_of = cfg.get("any_of", [])

        value = _jsonpath_first(trace.model_dump(mode="python"), field)
        text = "" if value is None else str(value)
        haystack = text if case_sensitive else text.lower()

        def _match(needle: str) -> bool:
            return (needle if case_sensitive else needle.lower()) in haystack

        matched_all_of = [s for s in all_of if _match(s)]
        matched_any_of = [s for s in any_of if _match(s)]
        matched_none_of = [s for s in none_of if _match(s)]

        all_ok = len(matched_all_of) == len(all_of)
        any_ok = len(any_of) == 0 or len(matched_any_of) > 0
        none_ok = len(matched_none_of) == 0
        passed = all_ok and any_ok and none_ok

        reason_parts: list[str] = []
        if not all_ok:
            missing = [s for s in all_of if s not in matched_all_of]
            reason_parts.append(f"missing required: {missing}")
        if not any_ok:
            reason_parts.append(f"none of any_of matched: {list(any_of)}")
        if not none_ok:
            reason_parts.append(f"forbidden present: {matched_none_of}")
        reason = "; ".join(reason_parts) if reason_parts else "ok"

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
                "matched_all_of": matched_all_of,
                "matched_any_of": matched_any_of,
                "matched_none_of": matched_none_of,
            },
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )


def _jsonpath_first(data: dict[str, Any], path: str) -> Any:
    matches = jsonpath_parse(path).find(data)
    if not matches:
        return None
    return matches[0].value
