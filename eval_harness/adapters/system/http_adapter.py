from __future__ import annotations

import json
import logging
import re
from types import TracebackType
from typing import Any, Self
from urllib.parse import urlparse

import httpx
from jsonpath_ng.ext import parse as jsonpath_parse

from eval_harness.adapters.system.presets import PRESETS
from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.errors import AdapterError, ConfigError, RetriableError
from eval_harness.core.models import EvalCase, RunVariant, Trace, TraceMetrics, TraceOutput
from eval_harness.core.time import utc_now

logger = logging.getLogger(__name__)

_TEMPLATE_RE = re.compile(r"\{\{\s*([^{}|]+?)(?:\s*\|\s*(json))?\s*\}\}")


class HttpSystemAdapter:
    name: str

    def __init__(
        self,
        name: str = "http",
        *,
        endpoint: str | None = None,
        method: str = "POST",
        timeout_seconds: float = 120,
        headers: dict[str, str] | None = None,
        query_params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        request_template: str | None = None,
        response_mapping: dict[str, str] | None = None,
        provider: str | None = None,
        enrich_trace_from: Any = None,
        stream: bool = False,
        metadata: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        if endpoint is None:
            raise ConfigError("http adapter requires 'endpoint'")
        _validate_url_scheme(endpoint)

        if stream:
            raise NotImplementedError(
                "http adapter: stream=true is out of scope for v0 (declared in pyproject.toml only)"
            )

        if enrich_trace_from:
            logger.warning(
                "http adapter '%s': enrich_trace_from is set but no TraceEnricher is "
                "implemented in v0; ignoring.",
                name,
            )

        preset_template: str | None = None
        preset_mapping: dict[str, str] = {}
        if provider is not None:
            if provider not in PRESETS:
                raise ConfigError(
                    f"http adapter '{name}': unknown provider '{provider}'. "
                    f"Known: {sorted(PRESETS)}"
                )
            preset = PRESETS[provider]
            preset_template = preset.get("request_template")
            preset_mapping = dict(preset.get("response_mapping", {}))

        merged_mapping: dict[str, str] = {**preset_mapping, **(response_mapping or {})}
        if not merged_mapping:
            raise ConfigError(
                f"http adapter '{name}': response_mapping is required unless provider is set"
            )

        self.name = name
        self._endpoint = endpoint
        self._method = method.upper()
        self._timeout = float(timeout_seconds)
        self._headers = dict(headers or {})
        self._query_params = dict(query_params or {})
        self._body = dict(body or {})
        self._request_template = request_template or preset_template
        self._response_mapping = merged_mapping
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> Self:
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        if self._client is None:
            raise AdapterError(
                "HttpSystemAdapter.run called outside of an `async with` context"
            )

        request_body = self._build_request_body(case)
        started_at = utc_now()
        try:
            response = await self._client.request(
                self._method,
                self._endpoint,
                params=self._query_params or None,
                headers=self._headers or None,
                json=request_body,
            )
            response.raise_for_status()
        except httpx.TimeoutException as e:
            raise RetriableError(f"http adapter '{self.name}': timeout: {e}") from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            msg = f"http adapter '{self.name}': HTTP {status} from {self._endpoint}"
            if status >= 500:
                raise RetriableError(msg) from e
            raise AdapterError(msg) from e
        except httpx.HTTPError as e:
            raise AdapterError(f"http adapter '{self.name}': transport error: {e}") from e

        finished_at = utc_now()
        latency_ms = max(int((finished_at - started_at).total_seconds() * 1000), 0)

        try:
            payload = response.json()
        except ValueError as e:
            raise AdapterError(
                f"http adapter '{self.name}': response is not JSON: {e}"
            ) from e

        return self._compose_trace(
            payload=payload,
            case=case,
            variant=variant,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
        )

    def _build_request_body(self, case: EvalCase) -> Any:
        template = self._request_template
        if template is None:
            base = dict(self._body)
            base.update(case.input)
            return base

        context = {
            "input": case.input,
            "case_id": case.id,
            "case": {"id": case.id, "input": case.input, "metadata": case.metadata},
        }
        rendered = _render_template(template, context)
        try:
            parsed = json.loads(rendered)
        except json.JSONDecodeError as e:
            raise AdapterError(
                f"http adapter '{self.name}': request_template did not render to valid "
                f"JSON: {e}; rendered={rendered!r}"
            ) from e
        if self._body and isinstance(parsed, dict):
            merged = dict(self._body)
            merged.update(parsed)
            return merged
        return parsed

    def _compose_trace(
        self,
        *,
        payload: Any,
        case: EvalCase,
        variant: RunVariant,
        started_at: Any,
        finished_at: Any,
        latency_ms: int,
    ) -> Trace:
        extracted = _apply_response_mapping(payload, self._response_mapping)

        final_answer = _coerce_text(extracted.get("final_answer"))
        thinking = _coerce_text(extracted.get("thinking"))
        tool_calls_raw = extracted.get("tool_calls")
        trace_id = extracted.get("trace_id")
        token_input = _coerce_int(extracted.get("tokens.input"))
        token_output = _coerce_int(extracted.get("tokens.output"))
        token_thinking = _coerce_int(extracted.get("tokens.thinking"))

        from eval_harness.core.models import ToolCall

        tool_calls: list[ToolCall] = []
        if isinstance(tool_calls_raw, list):
            for raw in tool_calls_raw:
                if isinstance(raw, dict):
                    tool_calls.append(_to_tool_call(raw))

        extra: dict[str, Any] = {}
        if trace_id is not None:
            extra["trace_id"] = trace_id

        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
            input=dict(case.input),
            output=TraceOutput(final_answer=final_answer, thinking=thinking),
            tool_calls=tool_calls,
            metrics=TraceMetrics(
                token_input=token_input,
                token_output=token_output,
                token_thinking=token_thinking,
            ),
            extra=extra,
        )


def _validate_url_scheme(url: str) -> None:
    try:
        parsed = urlparse(url)
    except ValueError as e:
        raise ConfigError(f"http adapter: invalid endpoint URL '{url}': {e}") from e
    scheme = parsed.scheme.lower()
    if scheme == "https":
        return
    if scheme == "http":
        host = (parsed.hostname or "").lower()
        if host == "localhost" or host == "127.0.0.1" or host.startswith("127."):
            return
        raise ConfigError(
            f"http adapter: plain http:// only allowed for localhost; got '{url}'"
        )
    raise ConfigError(
        f"http adapter: unsupported URL scheme '{scheme}' in '{url}' "
        f"(only https:// and http://localhost are allowed)"
    )


def _render_template(template: str, context: dict[str, Any]) -> str:
    def replace(match: re.Match[str]) -> str:
        expr = match.group(1).strip()
        as_json = match.group(2) == "json"
        value = _resolve_path(expr, context)
        if as_json:
            return json.dumps(value)
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        return json.dumps(value)

    return _TEMPLATE_RE.sub(replace, template)


def _resolve_path(expr: str, context: dict[str, Any]) -> Any:
    parts = expr.split(".")
    val: Any = context
    for part in parts:
        val = val.get(part) if isinstance(val, dict) else getattr(val, part, None)
        if val is None:
            return None
    return val


def _apply_response_mapping(payload: Any, mapping: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, expr in mapping.items():
        try:
            jp = jsonpath_parse(expr)
        except Exception as e:
            raise AdapterError(
                f"invalid JSONPath in response_mapping[{key!r}]: {expr!r}: {e}"
            ) from e
        matches = [m.value for m in jp.find(payload)]
        if not matches:
            out[key] = None
        elif len(matches) == 1:
            out[key] = matches[0]
        else:
            out[key] = matches
    return out


def _coerce_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [v for v in value if isinstance(v, str)]
        if not parts:
            return None
        return "\n".join(parts)
    return str(value)


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_tool_call(raw: dict[str, Any]) -> Any:
    from eval_harness.core.models import ToolCall

    name = raw.get("name") or raw.get("function", {}).get("name", "")
    arguments_raw = raw.get("arguments")
    if arguments_raw is None:
        arguments_raw = raw.get("input")
    if arguments_raw is None:
        fn = raw.get("function")
        if isinstance(fn, dict):
            arguments_raw = fn.get("arguments")
    if isinstance(arguments_raw, str):
        try:
            arguments = json.loads(arguments_raw)
        except json.JSONDecodeError:
            arguments = {"_raw": arguments_raw}
    elif isinstance(arguments_raw, dict):
        arguments = arguments_raw
    else:
        arguments = {}
    return ToolCall(id=raw.get("id"), name=str(name), arguments=arguments)


