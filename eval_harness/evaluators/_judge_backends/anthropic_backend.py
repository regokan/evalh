"""Anthropic judge backend. Optional install — `pip install 'eval-harness[anthropic]'`."""

from __future__ import annotations

import json
from typing import Any

from eval_harness.core.errors import ConfigError
from eval_harness.evaluators._judge_backends import JudgeParseError

_SYSTEM_PROMPT = (
    "You are a strict, terse evaluator. Read the user's prompt and respond "
    "ONLY with a single JSON object matching the requested schema. Do not "
    "wrap the JSON in code fences. Do not add commentary."
)


class AnthropicJudgeBackend:
    """Wraps `anthropic.AsyncAnthropic` for use as a JudgeBackend."""

    def __init__(self, model: str) -> None:
        try:
            from anthropic import AsyncAnthropic
            from anthropic.types import TextBlock
        except ImportError as e:
            raise ConfigError(
                "llm_judge: anthropic backend requested but the `anthropic` "
                "SDK is not installed. Install with: "
                "pip install 'eval-harness[anthropic]'"
            ) from e
        self.model = model
        self._client = AsyncAnthropic()
        self._text_block_cls = TextBlock

    async def judge(
        self,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int,
    ) -> dict[str, Any]:
        schema_blob = json.dumps(schema, indent=2)
        user_content = (
            f"{prompt}\n\n"
            "Respond with a single JSON object matching this JSON schema "
            "exactly. No prose, no code fences.\n\n"
            f"Schema:\n{schema_blob}"
        )
        resp = await self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_content}],
        )
        text_parts = [b.text for b in resp.content if isinstance(b, self._text_block_cls)]
        raw = "".join(text_parts).strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise JudgeParseError(
                f"anthropic backend: judge did not return valid JSON: {e}; "
                f"raw response: {raw[:500]}"
            ) from e
        if not isinstance(data, dict):
            raise JudgeParseError(
                f"anthropic backend: judge JSON was {type(data).__name__}, "
                f"expected object: {raw[:500]}"
            )
        usage = getattr(resp, "usage", None)
        if usage is not None:
            data.setdefault(
                "_usage",
                {
                    "input_tokens": getattr(usage, "input_tokens", 0),
                    "output_tokens": getattr(usage, "output_tokens", 0),
                },
            )
        return data
