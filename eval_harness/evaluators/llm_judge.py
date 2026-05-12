from __future__ import annotations

import json
import re
import traceback
from typing import Any, ClassVar

from jsonpath_ng import parse as jsonpath_parse

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
    TraceError,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators._judge_backends import (
    JudgeBackend,
    judge_backend_registry,
)
from eval_harness.evaluators._judge_backends._pricing import (
    estimate_cost_usd,
    estimate_tokens_from_text,
)
from eval_harness.evaluators.base import Evaluator

_DEFAULT_INCLUDE = ["input", "output.final_answer"]
_K_OF_N_RE = re.compile(r"^k_of_n\s*=\s*(\d+)$")
_VALID_PASS_WHEN = {"all", "any", "majority"}
_DEFAULT_MAX_TOKENS = 1024


class LlmJudgeEvaluator(Evaluator):
    type: ClassVar[str] = "llm_judge"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        model = config.get("model")
        if not isinstance(model, str) or not model:
            raise ConfigError("llm_judge: 'model' (string) is required")

        nl_assertions = config.get("nl_assertions")
        rubric = config.get("rubric")
        if nl_assertions is None and rubric is None:
            raise ConfigError(
                "llm_judge: at least one of 'nl_assertions' or 'rubric' must be set"
            )

        if nl_assertions is not None:
            _validate_assertions(nl_assertions)
            pass_when = config.get("pass_when", "all")
            _validate_pass_when(pass_when, len(nl_assertions))

        if rubric is not None:
            if not isinstance(rubric, str) or not rubric.strip():
                raise ConfigError("llm_judge: 'rubric' must be a non-empty string")
            scale = config.get("scale", {"min": 1, "max": 5})
            if (
                not isinstance(scale, dict)
                or not isinstance(scale.get("min"), int | float)
                or not isinstance(scale.get("max"), int | float)
                or scale["min"] >= scale["max"]
            ):
                raise ConfigError(
                    "llm_judge: 'scale' must be {min, max} with min < max"
                )
            threshold_key = (
                "rubric_pass_threshold" if nl_assertions is not None else "pass_threshold"
            )
            threshold = config.get(threshold_key)
            if threshold is None or not isinstance(threshold, int | float):
                raise ConfigError(
                    f"llm_judge: '{threshold_key}' (number) is required when rubric is set"
                )

        include = config.get("include_in_prompt", _DEFAULT_INCLUDE)
        if not isinstance(include, list) or not all(isinstance(s, str) for s in include):
            raise ConfigError("llm_judge: 'include_in_prompt' must be list[str]")

    def __init__(self, name: str, **config: Any) -> None:
        super().__init__(name, **config)
        model = str(config["model"])
        self._model: str = model
        self._nl_assertions = _normalize_assertions(config.get("nl_assertions"))
        self._rubric: str | None = config.get("rubric")
        self._scale: dict[str, float] = {
            "min": float(config.get("scale", {"min": 1, "max": 5}).get("min", 1)),
            "max": float(config.get("scale", {"min": 1, "max": 5}).get("max", 5)),
        }
        if self._nl_assertions is not None:
            self._pass_when: str = str(config.get("pass_when", "all"))
        else:
            self._pass_when = "all"
        if self._rubric is not None:
            self._rubric_threshold: float | None = float(
                config["rubric_pass_threshold"]
                if self._nl_assertions is not None
                else config["pass_threshold"]
            )
        else:
            self._rubric_threshold = None
        self._include = list(config.get("include_in_prompt", _DEFAULT_INCLUDE))
        self._cost_limit_usd: float | None = config.get("cost_limit_usd")
        self._schema_override: dict[str, Any] | None = config.get("judge_response_schema")
        self._max_tokens: int = int(config.get("max_tokens", _DEFAULT_MAX_TOKENS))

        # Resolve backend at plan time so a missing SDK fails before any case runs.
        self._backend: JudgeBackend = judge_backend_registry.resolve(model)

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()
        schema = self._schema_override or _auto_schema(
            self._nl_assertions, self._rubric is not None
        )
        prompt = _build_prompt(
            case=case,
            trace=trace,
            include=self._include,
            nl_assertions=self._nl_assertions,
            rubric=self._rubric,
            scale=self._scale,
            pass_when=self._pass_when,
        )

        if self._cost_limit_usd is not None:
            estimated_input = estimate_tokens_from_text(prompt)
            estimated_output = min(self._max_tokens, 512)
            estimated = estimate_cost_usd(self._model, estimated_input, estimated_output)
            if estimated > self._cost_limit_usd:
                return _error_result(
                    self,
                    case,
                    trace,
                    started,
                    type_="cost_limit_exceeded",
                    message=(
                        f"estimated cost ${estimated:.4f} exceeds cost_limit_usd "
                        f"${self._cost_limit_usd:.4f}"
                    ),
                )

        try:
            judge_response = await self._backend.judge(prompt, schema, self._max_tokens)
        except Exception as e:
            return _error_result(
                self,
                case,
                trace,
                started,
                type_="adapter_error",
                message=f"judge call failed: {type(e).__name__}: {e}",
                stack=traceback.format_exc(),
            )

        try:
            passed, score, reason, detail = _aggregate(
                judge_response=judge_response,
                nl_assertions=self._nl_assertions,
                rubric_present=self._rubric is not None,
                pass_when=self._pass_when,
                rubric_threshold=self._rubric_threshold,
                scale=self._scale,
            )
        except Exception as e:
            return _error_result(
                self,
                case,
                trace,
                started,
                type_="adapter_error",
                message=f"judge response parse failed: {type(e).__name__}: {e}",
                stack=traceback.format_exc(),
            )

        detail["judge_model"] = self._model
        usage = judge_response.get("_usage") if isinstance(judge_response, dict) else None
        if isinstance(usage, dict):
            actual = estimate_cost_usd(
                self._model,
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
            )
            detail["cost_usd"] = round(actual, 6)
            if self._cost_limit_usd is not None and actual > self._cost_limit_usd:
                return _error_result(
                    self,
                    case,
                    trace,
                    started,
                    type_="cost_limit_exceeded",
                    message=(
                        f"actual cost ${actual:.4f} exceeds cost_limit_usd "
                        f"${self._cost_limit_usd:.4f}"
                    ),
                )

        finished = utc_now()
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=passed,
            score=score,
            reason=reason,
            detail=detail,
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )


def _validate_assertions(value: Any) -> None:
    if not isinstance(value, list) or not value:
        raise ConfigError("llm_judge: 'nl_assertions' must be a non-empty list")
    for i, item in enumerate(value):
        if isinstance(item, str):
            if not item.strip():
                raise ConfigError(f"llm_judge: nl_assertions[{i}] is empty")
            continue
        if isinstance(item, dict):
            text = item.get("text")
            required = item.get("required", True)
            if not isinstance(text, str) or not text.strip():
                raise ConfigError(
                    f"llm_judge: nl_assertions[{i}].text must be a non-empty string"
                )
            if not isinstance(required, bool):
                raise ConfigError(
                    f"llm_judge: nl_assertions[{i}].required must be bool"
                )
            continue
        raise ConfigError(
            f"llm_judge: nl_assertions[{i}] must be a string or "
            f"{{text, required}} dict"
        )


def _validate_pass_when(value: Any, n_assertions: int) -> None:
    if not isinstance(value, str):
        raise ConfigError("llm_judge: 'pass_when' must be a string")
    if value in _VALID_PASS_WHEN:
        return
    m = _K_OF_N_RE.match(value)
    if not m:
        raise ConfigError(
            f"llm_judge: 'pass_when' must be one of {_VALID_PASS_WHEN | {'k_of_n=N'}}; "
            f"got {value!r}"
        )
    k = int(m.group(1))
    if k <= 0 or k > n_assertions:
        raise ConfigError(
            f"llm_judge: 'k_of_n={k}' is out of range for {n_assertions} assertions"
        )


def _normalize_assertions(value: Any) -> list[dict[str, Any]] | None:
    if value is None:
        return None
    out: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, str):
            out.append({"text": item, "required": True})
        else:
            out.append(
                {"text": item["text"], "required": bool(item.get("required", True))}
            )
    return out


def _auto_schema(
    nl_assertions: list[dict[str, Any]] | None, rubric_present: bool
) -> dict[str, Any]:
    props: dict[str, Any] = {}
    required: list[str] = []
    if nl_assertions is not None:
        props["assertions"] = {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "passed": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["text", "passed", "reason"],
            },
        }
        required.append("assertions")
    if rubric_present:
        props["score"] = {"type": "number"}
        props["rubric_reason"] = {"type": "string"}
        required.extend(["score", "rubric_reason"])
    return {"type": "object", "properties": props, "required": required}


def _build_prompt(
    *,
    case: EvalCase,
    trace: Trace,
    include: list[str],
    nl_assertions: list[dict[str, Any]] | None,
    rubric: str | None,
    scale: dict[str, float],
    pass_when: str,
) -> str:
    payload = trace.model_dump(mode="python")
    payload["case"] = case.model_dump(mode="python")

    sections: list[str] = []
    sections.append(
        "You are grading a single AI system response. Use ONLY the information "
        "provided below. Do not assume facts not present."
    )
    sections.append("=== Trace fields ===")
    for path in include:
        value = _jsonpath_first(payload, path)
        sections.append(f"[{path}]\n{_format_value(value)}")
    expected = case.expected.model_dump(mode="python")
    if any(v for v in expected.values() if v):
        sections.append("=== Case expected ===")
        sections.append(json.dumps(expected, default=str, indent=2))

    if nl_assertions is not None:
        sections.append("=== Assertions ===")
        sections.append(
            "Evaluate each assertion independently. For each, return passed "
            "(true/false) and a short reason citing the evidence."
        )
        for i, a in enumerate(nl_assertions, 1):
            tag = "required" if a["required"] else "optional"
            sections.append(f"{i}. ({tag}) {a['text']}")
        sections.append(f"Overall pass criterion: {pass_when}.")

    if rubric is not None:
        sections.append("=== Rubric ===")
        sections.append(rubric)
        sections.append(
            f"Provide a numeric `score` in [{scale['min']}, {scale['max']}] and a "
            "short `rubric_reason`."
        )

    return "\n\n".join(sections)


def _format_value(value: Any) -> str:
    if value is None:
        return "(missing)"
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str, indent=2)


def _jsonpath_first(data: dict[str, Any], path: str) -> Any:
    try:
        matches = jsonpath_parse(path).find(data)
    except Exception:
        return None
    return matches[0].value if matches else None


def _aggregate(
    *,
    judge_response: dict[str, Any],
    nl_assertions: list[dict[str, Any]] | None,
    rubric_present: bool,
    pass_when: str,
    rubric_threshold: float | None,
    scale: dict[str, float],
) -> tuple[bool, float | None, str, dict[str, Any]]:
    detail: dict[str, Any] = {}
    nl_passed = True
    nl_score: float | None = None
    nl_reason = "ok"

    if nl_assertions is not None:
        raw = judge_response.get("assertions")
        if not isinstance(raw, list) or len(raw) != len(nl_assertions):
            raise ValueError(
                f"expected {len(nl_assertions)} assertion verdicts; got {raw!r}"
            )
        per_assertion: list[dict[str, Any]] = []
        for spec, verdict in zip(nl_assertions, raw, strict=True):
            if not isinstance(verdict, dict):
                raise ValueError(f"verdict not a dict: {verdict!r}")
            passed = bool(verdict.get("passed"))
            per_assertion.append(
                {
                    "text": spec["text"],
                    "required": spec["required"],
                    "passed": passed,
                    "reason": str(verdict.get("reason", "")),
                }
            )
        detail["assertions"] = per_assertion
        nl_passed, nl_score, nl_reason = _aggregate_assertions(
            per_assertion, pass_when
        )

    rubric_passed = True
    rubric_reason = ""
    if rubric_present:
        if "score" not in judge_response:
            raise ValueError("judge response missing 'score' for rubric mode")
        try:
            score = float(judge_response["score"])
        except (TypeError, ValueError) as e:
            raise ValueError(f"rubric score is not numeric: {judge_response['score']!r}") from e
        rubric_reason = str(judge_response.get("rubric_reason", ""))
        if score < scale["min"] or score > scale["max"]:
            raise ValueError(
                f"rubric score {score} out of scale [{scale['min']}, {scale['max']}]"
            )
        assert rubric_threshold is not None
        rubric_passed = score >= rubric_threshold
        detail["rubric"] = {
            "score": score,
            "reason": rubric_reason,
            "threshold": rubric_threshold,
        }

    overall_passed = nl_passed and rubric_passed
    if nl_assertions is not None and rubric_present:
        score_value = nl_score
        reason = f"nl_assertions: {nl_reason}; rubric: {'pass' if rubric_passed else 'fail'}"
    elif nl_assertions is not None:
        score_value = nl_score
        reason = nl_reason
    else:
        score_value = None
        reason = f"rubric: {'pass' if rubric_passed else 'fail'}: {rubric_reason}"
    return overall_passed, score_value, reason, detail


def _aggregate_assertions(
    per_assertion: list[dict[str, Any]], pass_when: str
) -> tuple[bool, float, str]:
    total = len(per_assertion)
    passed_count = sum(1 for a in per_assertion if a["passed"])
    score = passed_count / total if total else 0.0

    required_total = sum(1 for a in per_assertion if a["required"])
    required_passed = sum(1 for a in per_assertion if a["required"] and a["passed"])
    optional_failed = [a for a in per_assertion if not a["required"] and not a["passed"]]

    if pass_when == "all":
        passed = required_passed == required_total
    elif pass_when == "any":
        passed = any(a["passed"] for a in per_assertion if a["required"]) or (
            required_total == 0 and passed_count > 0
        )
    elif pass_when == "majority":
        passed = passed_count * 2 > total
    else:
        m = _K_OF_N_RE.match(pass_when)
        assert m is not None
        k = int(m.group(1))
        passed = passed_count >= k

    if passed:
        if optional_failed:
            reason = (
                f"{passed_count}/{total} assertions passed (optional misses: "
                f"{[a['text'] for a in optional_failed]})"
            )
        else:
            reason = f"{passed_count}/{total} assertions passed"
    else:
        failures = [a for a in per_assertion if a["required"] and not a["passed"]]
        reason = (
            f"{total - passed_count}/{total} assertions failed: "
            f"{[a['text'] for a in failures]}"
        )
    return passed, score, reason


def _error_result(
    evaluator: LlmJudgeEvaluator,
    case: EvalCase,
    trace: Trace,
    started: Any,
    *,
    type_: str,
    message: str,
    stack: str | None = None,
) -> EvaluationResult:
    finished = utc_now()
    return EvaluationResult(
        run_id=trace.run_id,
        case_id=case.id,
        variant_name=trace.variant_name,
        evaluator=evaluator.name,
        evaluator_type=evaluator.type,
        passed=False,
        score=None,
        reason=message,
        detail={"judge_model": evaluator._model},
        started_at=started,
        finished_at=finished,
        latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        error=TraceError(type=type_, message=message, stack=stack),
    )
