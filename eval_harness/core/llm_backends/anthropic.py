"""Anthropic LlmBackend. Optional install — `pip install 'eval-harness[anthropic]'`."""

from __future__ import annotations

import json
from typing import Any

from eval_harness.core.errors import ConfigError
from eval_harness.core.llm_backends import (
    LlmCall,
    LlmCallCostLimitError,
    LlmParseError,
)
from eval_harness.core.llm_backends._pricing import (
    estimate_cost_usd,
    estimate_tokens_from_text,
)

_DEFAULT_SYSTEM = (
    "You are a precise assistant. Read the user's prompt and respond only with "
    "what was requested — when a JSON schema is provided, return a single JSON "
    "object matching it exactly, no code fences, no commentary."
)


class AnthropicLlmBackend:
    """Wraps ``anthropic.AsyncAnthropic`` as an LlmBackend.

    Subsumes the v0.x ``AnthropicJudgeBackend`` logic. When a ``schema`` is
    passed to ``generate`` the prompt is augmented with the JSON-schema
    contract and the structured response is parsed back into
    ``LlmCall.structured``.
    """

    def __init__(self, model: str) -> None:
        try:
            from anthropic import AsyncAnthropic
            from anthropic.types import TextBlock
        except ImportError as e:
            raise ConfigError(
                "llm backend: anthropic SDK not installed. "
                "Install with: pip install 'eval-harness[anthropic]'"
            ) from e
        self.model = model
        self._client = AsyncAnthropic()
        self._text_block_cls = TextBlock

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        system: str | None = None,
        schema: dict[str, Any] | None = None,
        cost_limit_usd: float | None = None,
    ) -> LlmCall:
        user_content = prompt
        if schema is not None:
            user_content = (
                f"{prompt}\n\nRespond with a single JSON object matching this "
                f"JSON schema exactly. No prose, no code fences.\n\n"
                f"Schema:\n{json.dumps(schema, indent=2)}"
            )

        if cost_limit_usd is not None:
            estimated_input = estimate_tokens_from_text(user_content)
            estimated_output = min(max_tokens, 512)
            estimated = estimate_cost_usd(
                self.model, estimated_input, estimated_output
            )
            if estimated > cost_limit_usd:
                raise LlmCallCostLimitError(estimated, cost_limit_usd)

        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system or _DEFAULT_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        text_parts = [
            b.text for b in resp.content if isinstance(b, self._text_block_cls)
        ]
        raw = "".join(text_parts).strip()

        structured: dict[str, Any] | None = None
        if schema is not None:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise LlmParseError(
                    f"anthropic backend: response was not valid JSON: {e}; "
                    f"raw response: {raw[:500]}"
                ) from e
            if not isinstance(data, dict):
                raise LlmParseError(
                    f"anthropic backend: JSON was {type(data).__name__}, "
                    f"expected object: {raw[:500]}"
                )
            structured = data

        usage = getattr(resp, "usage", None)
        token_input = getattr(usage, "input_tokens", None) if usage else None
        token_output = getattr(usage, "output_tokens", None) if usage else None
        cost_usd = (
            estimate_cost_usd(self.model, int(token_input), int(token_output))
            if token_input is not None and token_output is not None
            else None
        )

        return LlmCall(
            text=raw,
            structured=structured,
            token_input=token_input,
            token_output=token_output,
            cost_usd=cost_usd,
        )
