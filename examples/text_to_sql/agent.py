"""Text-to-SQL agent for the text_to_sql Eval Harness example.

The agent receives a natural-language question, produces a JSON envelope
``{"sql": "...", "intent": "..."}``, writes the SQL to ``query.sql`` and
the case's chosen expected CSV to ``expected.csv`` in the workspace, and
copies ``compare.py`` alongside so the command evaluator can grade by
executing the SQL against the seeded fixture DB.

Default mode is OFFLINE: a deterministic stub maps each shipped case to
its canonical SQL. Setting ``EVALH_TEXT_TO_SQL_USE_LLM=1`` with
``ANTHROPIC_API_KEY`` exported swaps the stub for Claude.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from eval_harness.core.errors import ConfigError

_LLM_MODEL = "claude-haiku-4-5-20251001"
_LLM_MAX_TOKENS = 512
_COMPARE_PY = Path(__file__).parent / "compare.py"

_SYSTEM_PROMPT = (
    "You write SQLite SQL for a tiny customers/orders schema:\n"
    "  customers(id, name, email, signup_date)\n"
    "  orders(id, customer_id, amount, order_date)\n"
    "Reply with ONLY a JSON object of the form "
    '{"sql": "<one SQL statement>", "intent": "<short label>"}. '
    "The SQL must be a single SELECT statement, executable as-is on the "
    "schema above. No prose outside the JSON."
)

# Deterministic offline mapping. Keyed by case.id — the stub answers
# exactly the three shipped cases. Anything else falls through to a
# clearly-marked failure so the trace records why.
_STUB: dict[str, dict[str, str]] = {
    "total_revenue": {
        "sql": "SELECT SUM(amount) FROM orders;",
        "intent": "sum_order_amounts",
    },
    "customer_count": {
        "sql": "SELECT COUNT(*) FROM customers;",
        "intent": "count_customers",
    },
    "top_spender": {
        "sql": (
            "SELECT c.name, SUM(o.amount) "
            "FROM customers c JOIN orders o ON c.id = o.customer_id "
            "GROUP BY c.id "
            "ORDER BY 2 DESC LIMIT 1;"
        ),
        "intent": "rank_customer_total_spend",
    },
}


async def run(
    case: dict[str, Any], variant: dict[str, Any]
) -> dict[str, Any]:
    workspace_path = variant.get("_workspace_path")
    if not workspace_path:
        return _failure("python_function adapter did not provide _workspace_path")
    workspace = Path(workspace_path)

    expected_rel = case["input"].get("expected_csv")
    if not expected_rel:
        return _failure("case.input.expected_csv is required")
    expected_src = workspace / expected_rel
    if not expected_src.is_file():
        return _failure(
            f"expected CSV not present in workspace: {expected_rel}"
        )

    question = case["input"]["question"]

    if _use_llm():
        envelope, metrics = await _llm_envelope(question)
    else:
        envelope, metrics = _stub_envelope(case["id"]), {}

    if envelope is None:
        return _failure("agent failed to produce a SQL envelope")

    # Stage the workspace for the command evaluator.
    (workspace / "query.sql").write_text(envelope["sql"] + "\n", encoding="utf-8")
    shutil.copy(expected_src, workspace / "expected.csv")
    shutil.copy(_COMPARE_PY, workspace / "compare.py")

    return {
        "final_answer": (
            f"Wrote {envelope['intent']}: {envelope['sql']}"
        ),
        "structured": envelope,
        "metrics": metrics,
    }


def _use_llm() -> bool:
    return (
        os.environ.get("EVALH_TEXT_TO_SQL_USE_LLM") == "1"
        and bool(os.environ.get("ANTHROPIC_API_KEY"))
    )


def _stub_envelope(case_id: str) -> dict[str, str] | None:
    return _STUB.get(case_id)


async def _llm_envelope(
    question: str,
) -> tuple[dict[str, str] | None, dict[str, int]]:
    try:
        from anthropic import AsyncAnthropic
    except ImportError as e:
        raise ConfigError(
            "EVALH_TEXT_TO_SQL_USE_LLM=1 requires the `anthropic` extra. "
            "Install with: pip install 'eval-harness[anthropic]'"
        ) from e

    client = AsyncAnthropic()
    resp = await client.messages.create(
        model=_LLM_MODEL,
        max_tokens=_LLM_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": question}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    parsed = _extract_json(text)
    metrics = {
        "token_input": resp.usage.input_tokens,
        "token_output": resp.usage.output_tokens,
    }
    if not _is_envelope(parsed):
        # Returning the malformed value lets schema_match record the
        # exact violation rather than blanking the trace.
        return parsed if isinstance(parsed, dict) else None, metrics
    return parsed, metrics


def _is_envelope(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("sql"), str)
        and isinstance(value.get("intent"), str)
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


def _failure(message: str) -> dict[str, Any]:
    return {
        "final_answer": f"(agent failed) {message}",
        "metrics": {},
    }
