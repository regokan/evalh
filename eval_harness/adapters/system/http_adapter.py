from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncGenerator, Callable
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

_VALID_STREAM_FORMATS = ("sse", "json_lines", "raw_chunks")
_SSE_DONE_SENTINEL = "[DONE]"

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
        stream_format: str | None = None,
        stream_event_field: str | None = None,
        stream_done_field: str | None = None,
        stream_tool_call_field: str | None = None,
        stream_event_mapping: dict[str, str] | None = None,
        clock: Callable[[], float] | None = None,
        metadata: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        if endpoint is None:
            raise ConfigError("http adapter requires 'endpoint'")
        _validate_url_scheme(endpoint)

        if stream:
            if stream_format not in _VALID_STREAM_FORMATS:
                raise ConfigError(
                    f"http adapter '{name}': stream_format must be one of "
                    f"{_VALID_STREAM_FORMATS}, got {stream_format!r}"
                )
            if stream_format != "raw_chunks" and not stream_event_field:
                raise ConfigError(
                    f"http adapter '{name}': stream_event_field is required "
                    f"for stream_format={stream_format!r}"
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
        if not stream and not merged_mapping:
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
        self._stream = stream
        self._stream_format = stream_format
        self._stream_event_field = stream_event_field
        self._stream_done_field = stream_done_field
        self._stream_tool_call_field = stream_tool_call_field
        self._stream_event_mapping = dict(stream_event_mapping or {})
        self._clock: Callable[[], float] = clock or time.monotonic
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
        if self._stream:
            return await self._run_streaming(case, variant, request_body)

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

    async def _run_streaming(
        self,
        case: EvalCase,
        variant: RunVariant,
        request_body: Any,
    ) -> Trace:
        assert self._client is not None  # checked by caller
        started_at = utc_now()
        t0 = self._clock()
        first_token_ts: float | None = None
        last_token_ts = t0
        tokens: list[str] = []
        chunk_count = 0
        completed = False
        tool_calls_raw: list[dict[str, Any]] = []

        from eval_harness.core.models import ToolCall

        try:
            async with self._client.stream(
                self._method,
                self._endpoint,
                params=self._query_params or None,
                headers=self._headers or None,
                json=request_body,
            ) as response:
                response.raise_for_status()
                events = self._iter_events(response)
                try:
                    async for event in events:
                        chunk_count += 1
                        now = self._clock()
                        if first_token_ts is None:
                            first_token_ts = now
                        last_token_ts = now
                        token, is_done, tool_call_delta = self._extract_event_fields(
                            event
                        )
                        if token is not None:
                            tokens.append(token)
                        if tool_call_delta is not None and isinstance(
                            tool_call_delta, dict
                        ):
                            tool_calls_raw.append(tool_call_delta)
                        if is_done:
                            completed = True
                            break
                finally:
                    await events.aclose()
        except httpx.TimeoutException as e:
            raise RetriableError(f"http adapter '{self.name}': stream timeout: {e}") from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            msg = f"http adapter '{self.name}': stream HTTP {status} from {self._endpoint}"
            if status >= 500:
                raise RetriableError(msg) from e
            raise AdapterError(msg) from e
        except httpx.HTTPError as e:
            raise AdapterError(
                f"http adapter '{self.name}': stream transport error: {e}"
            ) from e

        finished_at = utc_now()
        latency_ms = max(int((finished_at - started_at).total_seconds() * 1000), 0)

        latency_first_token_ms: int | None = (
            int((first_token_ts - t0) * 1000) if first_token_ts is not None else None
        )
        total_stream_ms = int((last_token_ts - t0) * 1000)
        latency_last_token_ms = total_stream_ms if chunk_count else None
        final_answer = "".join(tokens) if tokens else None
        token_output = len(tokens) if tokens else None
        tps: float | None
        if token_output and total_stream_ms > 0:
            tps = round(token_output / (total_stream_ms / 1000.0), 4)
        else:
            tps = None

        tool_calls: list[ToolCall] = [_to_tool_call(raw) for raw in tool_calls_raw]

        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=started_at,
            finished_at=finished_at,
            latency_ms=latency_ms,
            input=dict(case.input),
            output=TraceOutput(final_answer=final_answer),
            tool_calls=tool_calls,
            metrics=TraceMetrics(
                token_output=token_output,
                latency_first_token_ms=latency_first_token_ms,
                latency_last_token_ms=latency_last_token_ms,
                tokens_per_second=tps,
                stream_chunks=chunk_count,
                stream_completed=completed,
            ),
        )

    async def _iter_events(
        self, response: httpx.Response
    ) -> AsyncGenerator[Any, None]:
        fmt = self._stream_format
        if fmt == "sse":
            async for line in response.aiter_lines():
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                payload = line[len("data:") :].strip()
                if payload == _SSE_DONE_SENTINEL:
                    yield {"_done": True}
                    return
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    # Non-JSON SSE payload — surface as a raw-text event so
                    # raw-mapped configs still get a token.
                    yield {"_raw_text": payload}
        elif fmt == "json_lines":
            async for line in response.aiter_lines():
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    yield json.loads(stripped)
                except json.JSONDecodeError as e:
                    raise AdapterError(
                        f"http adapter '{self.name}': json_lines event is not "
                        f"valid JSON: {e}; line={stripped[:200]!r}"
                    ) from e
        elif fmt == "raw_chunks":
            async for raw in response.aiter_text():
                if raw:
                    yield {"_raw_text": raw}
        else:
            raise AdapterError(
                f"http adapter '{self.name}': unknown stream_format {fmt!r}"
            )

    def _extract_event_fields(
        self, event: Any
    ) -> tuple[str | None, bool, dict[str, Any] | None]:
        """Return ``(token, is_done, tool_call_delta)`` for one stream event."""
        if isinstance(event, dict) and event.get("_done") is True:
            return None, True, None

        if self._stream_format == "raw_chunks":
            if isinstance(event, dict) and "_raw_text" in event:
                return str(event["_raw_text"]), False, None
            return None, False, None

        if isinstance(event, dict) and "_raw_text" in event:
            return str(event["_raw_text"]), False, None

        if self._stream_event_mapping:
            extracted = _apply_response_mapping(event, self._stream_event_mapping)
            token = _coerce_text(extracted.get("token"))
            done_val = extracted.get("done")
            tool_delta = extracted.get("tool_call")
        else:
            token = (
                _jsonpath_first(event, self._stream_event_field)
                if self._stream_event_field
                else None
            )
            token = _coerce_text(token)
            done_val = (
                _jsonpath_first(event, self._stream_done_field)
                if self._stream_done_field
                else None
            )
            tool_delta = (
                _jsonpath_first(event, self._stream_tool_call_field)
                if self._stream_tool_call_field
                else None
            )

        is_done = done_val is not None and done_val is not False and done_val != ""
        tool_call_delta = tool_delta if isinstance(tool_delta, dict) else None
        return token, is_done, tool_call_delta


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


def _jsonpath_first(payload: Any, expr: str) -> Any:
    try:
        jp = jsonpath_parse(expr)
    except Exception:
        return None
    matches = [m.value for m in jp.find(payload)]
    if not matches:
        return None
    return matches[0]


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


