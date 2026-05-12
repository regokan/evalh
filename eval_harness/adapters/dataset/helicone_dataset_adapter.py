"""Helicone DatasetAdapter — pull historical request logs as eval cases.

Supports `embed_full_trace: true` so the v1 `replay` SystemAdapter can
score historical Helicone traffic offline. Auth + REST live in
`eval_harness._platforms.helicone.HeliconeClient`; this file is just
shape mapping (Helicone request log -> our `EvalCase` / `Trace`).
"""

from __future__ import annotations

import contextlib
import random
from datetime import datetime
from typing import Any

from eval_harness._platforms.helicone import (
    HeliconeClient,
    get_or_create_helicone_client,
    release_helicone_client,
)
from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import (
    EvalCase,
    Trace,
    TraceMessage,
    TraceMetrics,
    TraceOutput,
)
from eval_harness.core.time import utc_now


class HeliconeDatasetAdapter:
    embed_full_trace: bool

    def __init__(
        self,
        *,
        api_key: str | None = None,
        host: str | None = None,
        filter: dict[str, Any] | None = None,
        sample: int | None = None,
        embed_full_trace: bool = False,
        client: HeliconeClient | None = None,
        **_extra: Any,
    ) -> None:
        if client is None and not api_key:
            raise ConfigError(
                "helicone dataset: 'api_key' is required when no client is "
                "injected (set via ${HELICONE_API_KEY} in eval.yaml)"
            )
        self._filter: dict[str, Any] = dict(filter or {})
        self._sample = sample
        self.embed_full_trace = embed_full_trace
        self._owns_client = client is None
        self._client: HeliconeClient = client or get_or_create_helicone_client(
            api_key=api_key or "", host=host
        )

    async def load_cases(self) -> list[EvalCase]:
        try:
            payloads = await self._client.search_requests(self._filter)
        except Exception as e:
            raise AdapterError(
                f"helicone dataset: search_requests failed: "
                f"{type(e).__name__}: {e}"
            ) from e

        cases: list[EvalCase] = [self._build_case(raw) for raw in payloads]

        if self._sample is not None and self._sample < len(cases):
            rng = random.Random(repr(sorted(self._filter.items())))
            cases = rng.sample(cases, self._sample)
        return cases

    def __del__(self) -> None:  # pragma: no cover — best-effort
        # __init__ can raise before assigning attrs; guard with getattr.
        if getattr(self, "_owns_client", False):
            with contextlib.suppress(Exception):
                release_helicone_client(self._client)

    def _build_case(self, raw: dict[str, Any]) -> EvalCase:
        case_id = str(
            raw.get("request_id")
            or raw.get("id")
            or raw.get("helicone_id")
            or ""
        )
        if not case_id:
            raise AdapterError(
                f"helicone dataset: request log has no id-like field: {raw!r}"
            )
        case_input = _extract_input(raw)
        metadata: dict[str, Any] = {
            "source": "helicone",
            "trace_id": case_id,
        }
        model = raw.get("model") or raw.get("request_model")
        if isinstance(model, str):
            metadata["model"] = model
        user_id = raw.get("user_id") or raw.get("user")
        if isinstance(user_id, str):
            metadata["user_id"] = user_id

        case = EvalCase(id=case_id, input=case_input, metadata=metadata)
        if self.embed_full_trace:
            case._embedded_trace = _upstream_to_trace(raw, case_id)
        return case


def _extract_input(raw: dict[str, Any]) -> dict[str, Any]:
    """Helicone request logs nest the prompt under `request_body` (OpenAI
    chat shape) or carry it inline. Try the most common positions; fall
    back to an empty dict if none match."""
    body = raw.get("request_body")
    if isinstance(body, dict):
        messages = body.get("messages")
        if isinstance(messages, list) and messages:
            # Coerce the last user-role message into `user_message` so it
            # plays nicely with our default trace `input` shape.
            user_msgs = [
                m for m in messages
                if isinstance(m, dict) and m.get("role") == "user"
            ]
            if user_msgs:
                last = user_msgs[-1]
                content = last.get("content")
                if isinstance(content, str):
                    return {"user_message": content, "messages": list(messages)}
            return {"messages": list(messages)}
        prompt = body.get("prompt") or body.get("input")
        if isinstance(prompt, str):
            return {"user_message": prompt}
        if isinstance(prompt, dict):
            return dict(prompt)
    # Top-level shortcuts (older Helicone shapes).
    inline = raw.get("prompt") or raw.get("input")
    if isinstance(inline, str):
        return {"user_message": inline}
    if isinstance(inline, dict):
        return dict(inline)
    return {}


def _upstream_to_trace(raw: dict[str, Any], case_id: str) -> Trace:
    started = (
        _parse_dt(raw.get("request_created_at"))
        or _parse_dt(raw.get("started_at"))
        or utc_now()
    )
    finished = (
        _parse_dt(raw.get("response_created_at"))
        or _parse_dt(raw.get("finished_at"))
        or started
    )
    latency_raw = raw.get("latency") or raw.get("latency_ms")
    if isinstance(latency_raw, int | float):
        latency_ms = int(latency_raw)
    else:
        latency_ms = max(0, int((finished - started).total_seconds() * 1000))

    final_answer: str | None = None
    response_body = raw.get("response_body")
    if isinstance(response_body, dict):
        choices = response_body.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict):
                msg = first.get("message")
                if isinstance(msg, dict):
                    content = msg.get("content")
                    if isinstance(content, str):
                        final_answer = content

    messages: list[TraceMessage] = []
    body = raw.get("request_body")
    if isinstance(body, dict):
        for m in body.get("messages", []) or []:
            if isinstance(m, dict):
                with contextlib.suppress(Exception):
                    messages.append(TraceMessage.model_validate(m))

    metrics = TraceMetrics(
        token_input=_maybe_int(raw.get("prompt_tokens")),
        token_output=_maybe_int(raw.get("completion_tokens")),
        cost_usd=_maybe_float(raw.get("cost") or raw.get("cost_usd")),
    )

    extra: dict[str, Any] = {
        "trace_id": case_id,
        "source_platform": "helicone",
    }
    if raw.get("model"):
        extra["model"] = raw["model"]

    return Trace(
        run_id=str(raw.get("run_id") or ""),
        case_id=case_id,
        variant_name=str(raw.get("variant_name") or "production"),
        started_at=started,
        finished_at=finished,
        latency_ms=latency_ms,
        input=_extract_input(raw),
        output=TraceOutput(final_answer=final_answer),
        messages=messages,
        tool_calls=[],
        tool_results=[],
        metrics=metrics,
        extra=extra,
    )


def _parse_dt(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _maybe_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return int(value)
    return None


def _maybe_float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int | float):
        return float(value)
    return None
