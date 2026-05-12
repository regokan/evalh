from __future__ import annotations

from typing import Any

PRESET: dict[str, Any] = {
    "request_template": (
        '{"messages": [{"role": "user", "content": {{ input.user_message | json }}}]}'
    ),
    "response_mapping": {
        "final_answer": '$.content[?(@.type=="text")].text',
        "thinking": '$.content[?(@.type=="thinking")].thinking',
        "tool_calls": '$.content[?(@.type=="tool_use")]',
        "tokens.input": "$.usage.input_tokens",
        "tokens.output": "$.usage.output_tokens",
        "tokens.thinking": "$.usage.thinking_tokens",
    },
}
