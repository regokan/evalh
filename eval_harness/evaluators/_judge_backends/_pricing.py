"""Coarse per-model pricing for the `cost_limit_usd` guard.

Intentionally imprecise (v0 has no shared cost table). Single safety net per
call; updates are PR-by-PR.
"""

from __future__ import annotations

# Rates are USD per million tokens.
_PRICING: dict[str, dict[str, float]] = {
    "claude-4-7": {"input": 3.0, "output": 15.0},
    "claude-haiku-4-5-20251001": {"input": 0.25, "output": 1.25},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "claude-opus-4-7": {"input": 15.0, "output": 75.0},
}

_DEFAULT = {"input": 3.0, "output": 15.0}


def estimate_tokens_from_text(text: str) -> int:
    """Coarse heuristic: ~4 chars per token. Intentionally imprecise."""
    return max(1, len(text) // 4)


def estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = _PRICING.get(model, _DEFAULT)
    return (
        (input_tokens / 1_000_000.0) * rates["input"]
        + (output_tokens / 1_000_000.0) * rates["output"]
    )
