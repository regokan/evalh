from __future__ import annotations

from typing import Any

from eval_harness.adapters.system.presets.anthropic_messages import (
    PRESET as anthropic_messages,
)
from eval_harness.adapters.system.presets.langserve_invoke import PRESET as langserve_invoke
from eval_harness.adapters.system.presets.openai_chat import PRESET as openai_chat
from eval_harness.adapters.system.presets.simple import PRESET as simple

PRESETS: dict[str, dict[str, Any]] = {
    "openai_chat": openai_chat,
    "anthropic_messages": anthropic_messages,
    "langserve_invoke": langserve_invoke,
    "simple": simple,
}

__all__ = ["PRESETS"]
