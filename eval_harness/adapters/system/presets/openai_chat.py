from __future__ import annotations

from typing import Any

PRESET: dict[str, Any] = {
    "request_template": (
        '{"messages": [{"role": "user", "content": {{ input.user_message | json }}}]}'
    ),
    "response_mapping": {
        "final_answer": "$.choices[0].message.content",
        "tool_calls": "$.choices[0].message.tool_calls",
        "tokens.input": "$.usage.prompt_tokens",
        "tokens.output": "$.usage.completion_tokens",
    },
}
