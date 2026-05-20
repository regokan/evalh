"""Deterministic stub agent for the observability_langfuse example.

The point of this example is *wiring* — DatasetAdapter, TraceStore, and
TraceEnricher all talking to the same upstream platform (Langfuse). The
agent itself is intentionally trivial so the example stays offline-runnable
and the reader's attention lands on the three observability patterns.

Output includes a stable ``trace_id`` echoed from the case so the
(commented-out) langfuse TraceEnricher has something to fetch with when
the user turns it on.
"""

from __future__ import annotations

from typing import Any

_LISTINGS: dict[str, dict[str, Any]] = {
    "ABC123": {"suburb": "Richmond",  "price": 1_350_000, "suburb_avg": 1_200_000},
    "XYZ789": {"suburb": "Brunswick", "price": 1_100_000, "suburb_avg":   950_000},
    "DEF456": {"suburb": "Carlton",   "price": 1_500_000, "suburb_avg": 1_450_000},
}


async def run(case: dict[str, Any], variant: dict[str, Any] | None = None) -> dict[str, Any]:
    listing_id = case["input"].get("listing_id") or _first_listing_id(case)
    info = _LISTINGS.get(listing_id, {"suburb": "Unknown", "price": 0, "suburb_avg": 0})
    answer = (
        f"Listing {listing_id} is in {info['suburb']}. It is listed at "
        f"${info['price']:,}; the {info['suburb']} average is "
        f"${info['suburb_avg']:,}."
    )
    return {
        "final_answer": answer,
        "tool_calls": [
            {"name": "get_listing_details", "arguments": {"listing_id": listing_id}},
            {"name": "get_average_suburb_price", "arguments": {"suburb": info["suburb"]}},
        ],
        "metrics": {"token_input": 80, "token_output": 24},
        "extra": {"trace_id": case.get("metadata", {}).get("upstream_trace_id", "")},
    }


def _first_listing_id(case: dict[str, Any]) -> str:
    msg = case["input"].get("user_message", "")
    for token in msg.split():
        stripped = token.strip(".,?!")
        if stripped in _LISTINGS:
            return stripped
    return next(iter(_LISTINGS))
