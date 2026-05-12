"""Anthropic LLM backend. Optional install — `pip install 'eval-harness[anthropic]'`."""

from __future__ import annotations

import json
from typing import Any

from eval_harness.core.errors import ConfigError
from eval_harness.core.llm_backends import LlmBackendParseError, LlmCall
from eval_harness.core.llm_backends._pricing import estimate_cost_usd

_SCHEMA_SYSTEM_PROMPT = (
    "You are a strict, terse evaluator. Read the user's prompt and respond "
    "ONLY with a single JSON object matching the requested schema. Do not "
    "wrap the JSON in code fences. Do not add commentary."
)


class AnthropicLlmBackend:
    """Wraps `anthropic.AsyncAnthropic` for the `LlmBackend` interface.

    Schema-aware: when a JSON schema is supplied, the backend appends the schema
    blob to the prompt and parses the response into `LlmCall.structured`. When
    no schema is supplied, the raw text is returned in `LlmCall.text`.
    """

    def __init__(self) -> None:
        try:
            from anthropic import AsyncAnthropic
            from anthropic.types import TextBlock
        except ImportError as e:
            raise ConfigError(
                "anthropic backend requested but the `anthropic` SDK is not "
                "installed. Install with: pip install 'eval-harness[anthropic]'"
            ) from e
        self._client = AsyncAnthropic()
        self._text_block_cls = TextBlock

    async def generate(
        self,
        prompt: str,
        *,
        model: str,
        max_tokens: int,
        system: str | None = None,
        schema: dict[str, Any] | None = None,
        cost_limit_usd: float | None = None,
    ) -> LlmCall:
        if schema is not None:
            schema_blob = json.dumps(schema, indent=2)
            user_content = (
                f"{prompt}\n\n"
                "Respond with a single JSON object matching this JSON schema "
                "exactly. No prose, no code fences.\n\n"
                f"Schema:\n{schema_blob}"
            )
            system_prompt: str | None = system or _SCHEMA_SYSTEM_PROMPT
        else:
            user_content = prompt
            system_prompt = system

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": user_content}],
        }
        if system_prompt is not None:
            kwargs["system"] = system_prompt

        resp = await self._client.messages.create(**kwargs)
        text_parts = [b.text for b in resp.content if isinstance(b, self._text_block_cls)]
        raw = "".join(text_parts).strip()

        structured: dict[str, Any] | None = None
        if schema is not None:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise LlmBackendParseError(
                    f"anthropic backend: response was not valid JSON: {e}; "
                    f"raw: {raw[:500]}"
                ) from e
            if not isinstance(data, dict):
                raise LlmBackendParseError(
                    f"anthropic backend: response JSON was {type(data).__name__}, "
                    f"expected object: {raw[:500]}"
                )
            structured = data

        usage = getattr(resp, "usage", None)
        token_input = int(getattr(usage, "input_tokens", 0)) if usage is not None else 0
        token_output = int(getattr(usage, "output_tokens", 0)) if usage is not None else 0
        cost_usd = estimate_cost_usd(model, token_input, token_output)

        return LlmCall(
            text=raw,
            structured=structured,
            token_input=token_input,
            token_output=token_output,
            cost_usd=cost_usd,
        )
