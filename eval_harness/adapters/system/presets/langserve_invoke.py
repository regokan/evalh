from __future__ import annotations

from typing import Any

PRESET: dict[str, Any] = {
    "request_template": '{"input": {{ input | json }}}',
    "response_mapping": {
        "final_answer": "$.output",
    },
}
