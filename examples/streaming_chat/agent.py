"""Streaming chat agent for the streaming_chat Eval Harness example.

The agent is an async generator over a fixed canned answer per case. It
synthesises the three streaming metrics the harness ships dedicated
evaluators for:

    trace.metrics.latency_first_token_ms
    trace.metrics.tokens_per_second
    trace.metrics.stream_completed

The metric values are derived from per-variant metadata
(``simulated_ttft_ms``, ``simulated_tps``) so the example is reproducible
without an LLM call or a wall-clock dependency. In a real chat agent
these numbers come from ``time.perf_counter()`` calls around the actual
SSE / async-iterator stream; the eval-harness contract is just "put a
number in the trace" — the source doesn't change which evaluators fire.

The example is fully offline: no API key, no network, no third-party
SDK. The token stream is iterated with a 0-duration ``asyncio.sleep`` so
the function is genuinely async without slowing the run.
"""

from __future__ import annotations

import asyncio
from typing import Any

_CANNED_ANSWERS: dict[str, str] = {
    "chat_paris_weather": (
        "Paris is currently mild with light cloud cover and a high near 18 "
        "degrees Celsius. Light rain is possible in the evening."
    ),
    "chat_usd_to_eur": (
        "At the current reference rate, 100 USD converts to roughly 92 EUR. "
        "Exact rates vary by provider and time of day."
    ),
    "chat_space_news": (
        "Recent space exploration news centres on a private crewed flight "
        "to low Earth orbit and a robotic mission returning samples from a "
        "near-Earth asteroid."
    ),
}


async def run(
    case: dict[str, Any], variant: dict[str, Any] | None = None
) -> dict[str, Any]:
    answer = _CANNED_ANSWERS.get(case["id"])
    if answer is None:
        raise ValueError(f"streaming_chat agent: no canned answer for case id {case['id']!r}")

    metadata = (variant or {}).get("metadata") or {}
    simulated_ttft_ms = int(metadata.get("simulated_ttft_ms", 200))
    simulated_tps = float(metadata.get("simulated_tps", 80.0))

    tokens = answer.split()
    # The "stream" — every token yielded one at a time, async-cooperative.
    # The body of the loop is intentionally trivial; the production shape
    # would feed each chunk to a UI / SSE writer / websocket.
    streamed: list[str] = []
    for tok in tokens:
        await asyncio.sleep(0)
        streamed.append(tok)

    final_answer = " ".join(streamed)
    n_tokens = len(streamed)

    # Synthesise the streaming metrics. In a real system: timestamp before
    # the first yield, again after the last, and divide by elapsed seconds.
    return {
        "final_answer": final_answer,
        "metrics": {
            "token_output": n_tokens,
            "latency_first_token_ms": simulated_ttft_ms,
            "tokens_per_second": simulated_tps,
            "stream_chunks": n_tokens,
            "stream_completed": True,
        },
    }
