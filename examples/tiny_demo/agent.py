"""Stub agent for the tiny_demo Eval Harness example.

This is a *real* agent — it calls Claude via the Anthropic SDK, lets the
model invoke tools, and returns the model's final answer. It is stochastic
by design: that's what Eval Harness is for.

What's "stub" about it is the tooling: `get_listing_details` and
`get_average_suburb_price` are backed by a hardcoded dict instead of a real
listings database, so the example is self-contained at the *infrastructure*
level (no DB to spin up, no HTTP service to start) while still being a
genuine LLM-driven agent.

Used by examples/tiny_demo/eval.yaml via the `python_function` SystemAdapter.

Requires `ANTHROPIC_API_KEY`. Picked up from either:
  - the shell environment, or
  - a `.env` file next to this script (examples/tiny_demo/.env).
The `.env` file is gitignored repo-wide; keep your key there for local runs.
"""

from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from dotenv import load_dotenv


# Load examples/tiny_demo/.env if present. Shell env wins over .env on conflict.
load_dotenv(Path(__file__).parent / ".env", override=False)


# Hardcoded "listings database" — three rows. Real-world: a Postgres query
# or an internal API call. The shape returned to the LLM matches what a real
# tool would return.
LISTINGS: dict[str, dict[str, Any]] = {
    "ABC123": {"suburb": "Richmond",  "price": 1_350_000, "suburb_avg": 1_200_000},
    "XYZ789": {"suburb": "Brunswick", "price": 1_100_000, "suburb_avg":   950_000},
    "DEF456": {"suburb": "Carlton",   "price": 1_500_000, "suburb_avg": 1_450_000},
}


TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_listing_details",
        "description": "Fetch suburb and price for a listing.",
        "input_schema": {
            "type": "object",
            "properties": {"listing_id": {"type": "string"}},
            "required": ["listing_id"],
        },
    },
    {
        "name": "get_average_suburb_price",
        "description": "Fetch the average house price for a suburb.",
        "input_schema": {
            "type": "object",
            "properties": {"suburb": {"type": "string"}},
            "required": ["suburb"],
        },
    },
]


SYSTEM_PROMPTS = {
    "concise": (
        "You are a real-estate assistant. Answer the user's question in "
        "2-3 sentences. Use the provided tools to fetch listing details "
        "and the suburb average."
    ),
    "verbose": (
        "You are a real-estate assistant. Answer thoroughly, walking through "
        "your reasoning step by step. Use the provided tools to fetch "
        "listing details and the suburb average."
    ),
}


# Cheap default. Override per variant by setting variant.metadata.model.
DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOOL_TURNS = 4


async def run(case: dict, variant: dict | None = None) -> dict:
    """Stub agent. Returns a Trace-shaped dict the python_function adapter
    will wrap into a Trace.

    `case["input"]["user_message"]` is the user prompt.
    `variant["metadata"]["style"]` selects the system prompt.
    `variant["metadata"]["model"]` (optional) overrides the model.
    """
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError(
            "tiny_demo/agent.py requires ANTHROPIC_API_KEY. Either export it in "
            "your shell, or drop it into examples/tiny_demo/.env (gitignored). "
            "This example calls a real LLM by design — Eval Harness is for "
            "evaluating stochastic systems."
        )

    style = (variant or {}).get("metadata", {}).get("style", "concise")
    model = (variant or {}).get("metadata", {}).get("model", DEFAULT_MODEL)
    user_message = case["input"]["user_message"]

    client = AsyncAnthropic()
    messages: list[dict[str, Any]] = [{"role": "user", "content": user_message}]
    tool_calls_recorded: list[dict[str, Any]] = []
    total_input_tokens = 0
    total_output_tokens = 0

    for _ in range(MAX_TOOL_TURNS):
        resp = await client.messages.create(
            model=model,
            max_tokens=1024,
            system=SYSTEM_PROMPTS[style],
            tools=TOOLS,
            messages=messages,
        )
        total_input_tokens += resp.usage.input_tokens
        total_output_tokens += resp.usage.output_tokens

        if resp.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": resp.content})
            tool_results: list[dict[str, Any]] = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                tool_calls_recorded.append({"name": block.name, "arguments": block.input})
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(_run_tool(block.name, block.input)),
                })
            messages.append({"role": "user", "content": tool_results})
            continue

        # Final answer turn.
        text_blocks = [b.text for b in resp.content if b.type == "text"]
        return {
            "final_answer": "\n".join(text_blocks).strip(),
            "tool_calls": tool_calls_recorded,
            "metrics": {
                "token_input": total_input_tokens,
                "token_output": total_output_tokens,
            },
        }

    return {
        "final_answer": "(agent did not converge within MAX_TOOL_TURNS)",
        "tool_calls": tool_calls_recorded,
        "metrics": {
            "token_input": total_input_tokens,
            "token_output": total_output_tokens,
        },
    }


def _run_tool(name: str, args: dict) -> dict:
    if name == "get_listing_details":
        info = LISTINGS.get(args["listing_id"])
        if info is None:
            return {"error": f"unknown listing: {args['listing_id']}"}
        return {"listing_id": args["listing_id"], "suburb": info["suburb"], "price": info["price"]}
    if name == "get_average_suburb_price":
        for info in LISTINGS.values():
            if info["suburb"].lower() == args["suburb"].lower():
                return {"suburb": args["suburb"], "average_price": info["suburb_avg"]}
        return {"error": f"unknown suburb: {args['suburb']}"}
    return {"error": f"unknown tool: {name}"}
