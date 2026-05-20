"""Deterministic stub agent for the regression_gate example.

This example exists to demonstrate the *run lifecycle* (`evalh run` ->
`evalh promote` -> `evalh drift`), not to evaluate a real LLM. The agent
returns canned answers driven by `case["metadata"]` so the example runs
offline in a fresh checkout — no API keys, no network, no flakiness.

The Trace shape mirrors what tiny_demo's real-LLM agent produces, so the
same evaluator set (`tool_called`, `contains_text`) fires identically.
"""

from __future__ import annotations

from typing import Any

LISTING_FACTS: dict[str, dict[str, Any]] = {
    "ABC123": {"suburb": "Richmond", "price": 1_350_000, "suburb_avg": 1_200_000},
    "XYZ789": {"suburb": "Brunswick", "price": 1_100_000, "suburb_avg": 950_000},
    "DEF456": {"suburb": "Carlton", "price": 1_500_000, "suburb_avg": 1_450_000},
}


async def run(case: dict, variant: dict | None = None) -> dict:
    listing_id = case["metadata"]["listing_id"]
    facts = LISTING_FACTS[listing_id]
    suburb = facts["suburb"]
    price = facts["price"]
    suburb_avg = facts["suburb_avg"]
    comparison = "above" if price > suburb_avg else "below"

    answer = (
        f"Listing {listing_id} is in {suburb}. The {suburb} average is "
        f"${suburb_avg:,}, and the listing is priced at ${price:,} — "
        f"{comparison} the suburb average."
    )

    return {
        "final_answer": answer,
        "tool_calls": [
            {"name": "get_listing_details", "arguments": {"listing_id": listing_id}},
            {"name": "get_average_suburb_price", "arguments": {"suburb": suburb}},
        ],
        "metrics": {"token_input": 0, "token_output": 0},
    }
