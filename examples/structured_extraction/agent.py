"""Invoice-extraction agent for the structured_extraction Eval Harness example.

Parses an unstructured invoice string into the canonical JSON envelope
defined by `schemas/invoice.schema.json`. The default extractor is a small
regex-based parser — fully offline, no API key. Setting
EVALH_EXTRACTION_USE_LLM=1 with ANTHROPIC_API_KEY swaps it for Claude.

The agent returns both fields the harness evaluators read:

  * `structured` — the parsed envelope. The python_function adapter places
    it on `output.structured`, where `schema_match` and `exact_match` look.
  * `final_answer` — a one-sentence natural-language summary. The
    `semantic_similarity` evaluator scores it against the per-case
    `reference_summary`.

Why a stub at all: this example is the anchor for `schema_match` and the
first appearance of `exact_match` in the repo. Both must be exercisable
without an API key so that CI's offline smoke can detect regressions in
the evaluator wiring itself, not just in whatever LLM happens to be cheap
this quarter.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "invoice.schema.json"
_LLM_MODEL = "claude-haiku-4-5-20251001"

_INVOICE_RE = re.compile(r"^Invoice\s+(\S+)", re.MULTILINE)
_DATE_RE = re.compile(r"^Date:\s*(\S+)", re.MULTILINE)
_CURRENCY_RE = re.compile(r"^Currency:\s*([A-Z]{3})", re.MULTILINE)
_TOTAL_RE = re.compile(r"^Total:\s*([0-9]+(?:\.[0-9]+)?)", re.MULTILINE)
_LINE_ITEM_RE = re.compile(
    r"^(?P<desc>[A-Za-z][^\n]*?)\s{2,}(?P<amount>[0-9]+(?:\.[0-9]+)?)\s*$",
    re.MULTILINE,
)


def _load_schema() -> dict[str, Any]:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _summarize(envelope: dict[str, Any]) -> str:
    items = envelope.get("line_items") or []
    descriptions = [str(it.get("description", "")).strip() for it in items]
    descriptions = [d for d in descriptions if d]
    if len(descriptions) == 0:
        body = "no line items"
    elif len(descriptions) == 1:
        body = descriptions[0].lower()
    elif len(descriptions) == 2:
        body = f"{descriptions[0].lower()} and {descriptions[1].lower()}"
    else:
        head = ", ".join(d.lower() for d in descriptions[:-1])
        body = f"{head}, and {descriptions[-1].lower()}"
    total = envelope.get("invoice_total")
    total_str = f"{total:g}" if isinstance(total, int | float) else str(total)
    return (
        f"{envelope.get('vendor', '')} invoice {envelope.get('invoice_id', '')} "
        f"dated {envelope.get('issue_date', '')} for {total_str} "
        f"{envelope.get('currency', '')} covers {body}."
    )


def _stub_extract(text: str) -> dict[str, Any]:
    """Deterministic regex extractor — used in offline mode."""
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    vendor = lines[0] if lines else ""

    invoice_match = _INVOICE_RE.search(text)
    date_match = _DATE_RE.search(text)
    currency_match = _CURRENCY_RE.search(text)
    total_match = _TOTAL_RE.search(text)

    line_items: list[dict[str, Any]] = []
    for m in _LINE_ITEM_RE.finditer(text):
        desc = m.group("desc").strip()
        if desc.lower().startswith(("invoice", "date:", "currency:", "total:")):
            continue
        line_items.append(
            {"description": desc, "amount": float(m.group("amount"))}
        )

    envelope: dict[str, Any] = {
        "invoice_id": invoice_match.group(1) if invoice_match else "",
        "vendor": vendor,
        "invoice_total": float(total_match.group(1)) if total_match else 0.0,
        "currency": currency_match.group(1) if currency_match else "USD",
        "line_items": line_items,
    }
    if date_match:
        envelope["issue_date"] = date_match.group(1)
    return envelope


async def _llm_extract(text: str) -> tuple[dict[str, Any], dict[str, int]]:
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:
        from eval_harness.core.errors import ConfigError

        raise ConfigError(
            "structured_extraction agent: EVALH_EXTRACTION_USE_LLM=1 requires "
            "the anthropic SDK. Install with: pip install 'eval-harness[anthropic]'"
        ) from e

    schema = _load_schema()
    client = AsyncAnthropic()
    prompt = (
        "Extract the invoice fields into JSON that conforms exactly to the "
        "schema below. Return ONLY the JSON object, no prose.\n\n"
        f"Schema:\n{json.dumps(schema, indent=2)}\n\n"
        f"Invoice:\n{text}"
    )
    resp = await client.messages.create(
        model=_LLM_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    envelope = json.loads(raw)
    return envelope, {
        "token_input": resp.usage.input_tokens,
        "token_output": resp.usage.output_tokens,
    }


async def run(
    case: dict[str, Any], variant: dict[str, Any] | None = None
) -> dict[str, Any]:
    del variant  # single-variant example.
    text = case["input"]["invoice_text"]

    use_llm = (
        os.environ.get("EVALH_EXTRACTION_USE_LLM") == "1"
        and bool(os.environ.get("ANTHROPIC_API_KEY"))
    )
    metrics: dict[str, int] = {}
    if use_llm:
        envelope, metrics = await _llm_extract(text)
    else:
        envelope = _stub_extract(text)

    return {
        "final_answer": _summarize(envelope),
        "structured": envelope,
        "metrics": metrics,
    }
