"""Support-bot agent for the Eval Harness multi_turn_support example.

The agent is a single Claude call per turn, conditioned on the running
`conversation` field that `user_simulator` synthesises into `case.input`.
Each invocation handles one assistant turn: read the conversation so far,
generate the next reply.

Requires `ANTHROPIC_API_KEY`. Optional dependency `anthropic` is guarded:
a missing install raises `ConfigError` at plan time rather than
ImportError at module import (sqlite_store pattern).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    from anthropic import AsyncAnthropic
except ImportError as _e:  # pragma: no cover — exercised when extra missing
    from eval_harness.core.errors import ConfigError

    raise ConfigError(
        "multi_turn_support: 'anthropic' is not installed. "
        "Install with: pip install 'eval-harness[anthropic]'"
    ) from _e

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:  # python-dotenv is optional; env vars work either way.
    pass

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 512

_SYSTEM_PROMPT = (
    "You are a mortgage-support agent. Be concrete and actionable. Cite the "
    "specific step the user should take next (a form name, a phone number, "
    "a date window). Do NOT ask for information the user has already given "
    "you in this conversation. If the user has stated a clear goal, work it "
    "to resolution within the turns available — do not defer with 'someone "
    "will be in touch'. Keep replies under three sentences."
)


async def run(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    del variant
    conversation = case["input"].get("conversation") or []
    if not conversation:
        # First turn: user_simulator hands us only the initial user_message.
        conversation = [
            {"role": "user", "content": case["input"]["user_message"]}
        ]

    messages = [
        {"role": _role_for_anthropic(m["role"]), "content": _content_text(m)}
        for m in conversation
        if m.get("role") in ("user", "assistant")
    ]

    client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = await client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=messages,
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()

    return {
        "final_answer": text,
        "metrics": {
            "token_input": resp.usage.input_tokens,
            "token_output": resp.usage.output_tokens,
        },
    }


def _role_for_anthropic(role: str) -> str:
    # user_simulator stores roles as "user"/"assistant" already; this keeps
    # the contract obvious if a future caller relabels.
    return "assistant" if role == "assistant" else "user"


def _content_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return str(content.get("text") or content.get("message") or content)
    return "" if content is None else str(content)
