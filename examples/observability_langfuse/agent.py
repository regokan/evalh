"""Deterministic support-bot stub for the observability_langfuse example.

The example is about the dataset / store / enricher wiring, not the
agent. This is the smallest possible agent: classify the user message
against a small topic table and emit a canned reply. Async, offline, no
LLM, no API key.

Each trace carries a synthetic ``extra.trace_id`` so the langfuse
enricher (commented out in eval.yaml by default) has something to look
up when you uncomment it. With no real Langfuse trace at that id the
enricher records the miss in ``trace.extra.enrichment_errors`` — the
intended failure-soft behaviour.
"""

from __future__ import annotations

from typing import Any

_REPLIES: dict[str, str] = {
    "rate_lock": (
        "Yes — most lenders allow a 30- to 60-day rate lock once your "
        "application is in underwriting. Confirm the exact window with "
        "your loan officer before submitting."
    ),
    "appraisal": (
        "Three options when an appraisal comes in low: (1) renegotiate the "
        "contract price, (2) cover the gap in cash, or (3) request a "
        "reconsideration of value with comparable sales."
    ),
    "pmi": (
        "PMI can be dropped once your loan-to-value ratio reaches 80% "
        "based on the original purchase price, or 78% automatically. "
        "Request a current valuation from your servicer to start."
    ),
}

_KEYWORDS: list[tuple[str, str]] = [
    ("rate_lock", "rate"),
    ("appraisal", "appraisal"),
    ("pmi", "pmi"),
]


async def run(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    del variant
    message = case["input"]["user_message"].lower()
    topic = next(
        (t for t, kw in _KEYWORDS if kw in message),
        "rate_lock",
    )
    answer = _REPLIES[topic]

    return {
        "final_answer": answer,
        "tool_calls": [
            {"name": "topic_classifier", "arguments": {"topic": topic}}
        ],
        "metrics": {"token_input": 0, "token_output": 0},
        # `trace.extra.trace_id` is the hook the LangfuseTraceEnricher reads.
        # In production this id comes from your real SystemAdapter (e.g. the
        # `http` adapter's response_mapping). Here we synthesise one so the
        # commented-out enricher in eval.yaml has a value to demonstrate.
        "extra": {
            "trace_id": f"demo-{case['id']}",
            "topic": topic,
        },
    }
