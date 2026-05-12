from __future__ import annotations

import re
from typing import Any, ClassVar

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.base import Evaluator

_TEMPLATE_RE = re.compile(r"\{\{\s*([a-zA-Z_][\w.]*)\s*\}\}")
_REGEX_PREFIX = "~="


class ToolCalledEvaluator(Evaluator):
    type: ClassVar[str] = "tool_called"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        if "tool_name" not in config:
            raise ConfigError(
                "tool_called: 'tool_name' is required (v0 does not yet support "
                "the case.expected.must_call_tools fallback)"
            )
        if not isinstance(config["tool_name"], str):
            raise ConfigError("tool_called: 'tool_name' must be a string")
        for key in ("must_appear", "must_succeed"):
            if key in config and not isinstance(config[key], bool):
                raise ConfigError(f"tool_called: '{key}' must be bool")
        if "min_calls" in config and not isinstance(config["min_calls"], int):
            raise ConfigError("tool_called: 'min_calls' must be int")
        if (
            "max_calls" in config
            and config["max_calls"] is not None
            and not isinstance(config["max_calls"], int)
        ):
            raise ConfigError("tool_called: 'max_calls' must be int or null")
        if (
            "args_match" in config
            and config["args_match"] is not None
            and not isinstance(config["args_match"], dict)
        ):
            raise ConfigError("tool_called: 'args_match' must be a mapping or null")

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()

        cfg = self._config
        tool_name: str = cfg["tool_name"]
        must_appear = bool(cfg.get("must_appear", True))
        min_calls = int(cfg.get("min_calls", 1))
        max_calls = cfg.get("max_calls")
        must_succeed = bool(cfg.get("must_succeed", True))
        args_match_raw = cfg.get("args_match")

        matching_calls = [tc for tc in trace.tool_calls if tc.name == tool_name]

        rendered_args: dict[str, Any] | None = None
        if args_match_raw is not None:
            rendered_args = _render_template(args_match_raw, case)
            matching_calls = [
                tc for tc in matching_calls if _args_match(tc.arguments, rendered_args)
            ]

        count = len(matching_calls)

        reason_parts: list[str] = []
        passed = True

        if must_appear:
            if count < min_calls:
                passed = False
                reason_parts.append(f"count {count} < min_calls {min_calls}")
            if max_calls is not None and count > max_calls:
                passed = False
                reason_parts.append(f"count {count} > max_calls {max_calls}")
        elif count > 0:
            passed = False
            reason_parts.append(f"must_appear=false but {count} matching call(s) present")

        failed_results: list[str] = []
        if passed and must_succeed and matching_calls:
            ids = {tc.id for tc in matching_calls if tc.id is not None}
            for tr in trace.tool_results:
                if tr.name != tool_name:
                    continue
                if ids and tr.tool_call_id not in ids:
                    continue
                if _is_error_result(tr.content):
                    failed_results.append(tr.tool_call_id or tr.name)
            if failed_results:
                passed = False
                reason_parts.append(f"tool errored for call(s): {failed_results}")

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
                "tool_name": tool_name,
                "count": count,
                "rendered_args_match": rendered_args,
                "failed_result_ids": failed_results,
            },
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )


def _is_error_result(content: dict[str, Any] | str) -> bool:
    if isinstance(content, str):
        return False
    return any(content.get(key) for key in ("error", "is_error"))


def _render_template(template: Any, case: EvalCase) -> Any:
    if isinstance(template, dict):
        return {k: _render_template(v, case) for k, v in template.items()}
    if isinstance(template, list):
        return [_render_template(v, case) for v in template]
    if isinstance(template, str):
        return _substitute(template, case)
    return template


def _substitute(value: str, case: EvalCase) -> Any:
    match = _TEMPLATE_RE.fullmatch(value.strip())
    if match:
        return _resolve(match.group(1), case)

    def _replace(m: re.Match[str]) -> str:
        resolved = _resolve(m.group(1), case)
        return "" if resolved is None else str(resolved)

    return _TEMPLATE_RE.sub(_replace, value)


def _resolve(dotted: str, case: EvalCase) -> Any:
    parts = dotted.split(".")
    if parts[0] != "case":
        return None
    cur: Any = case.model_dump(mode="python")
    for part in parts[1:]:
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _args_match(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    for key, exp in expected.items():
        if key not in actual:
            return False
        if not _value_match(actual[key], exp):
            return False
    return True


def _value_match(actual: Any, expected: Any) -> bool:
    if isinstance(expected, str) and expected.startswith(_REGEX_PREFIX):
        if not isinstance(actual, str):
            return False
        pattern = expected[len(_REGEX_PREFIX) :]
        return re.search(pattern, actual) is not None
    if isinstance(expected, dict) and isinstance(actual, dict):
        return _args_match(actual, expected)
    if isinstance(expected, list) and isinstance(actual, list):
        if len(actual) != len(expected):
            return False
        return all(_value_match(a, e) for a, e in zip(actual, expected, strict=True))
    return bool(actual == expected)
