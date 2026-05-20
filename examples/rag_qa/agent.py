"""RAG agent for the rag_qa Eval Harness example.

Two stages: a deterministic in-memory keyword retriever over the 12 docs
in fixtures/docs.jsonl, then a generator that composes an answer from the
retrieved documents. The default generator is a stub that concatenates
the first sentence of each retrieved doc — fully offline, no API key.
Setting EVALH_RAG_USE_LLM=1 with ANTHROPIC_API_KEY swaps the stub for
Claude.

The retriever exposes itself as a single tool call per case so the
`tool_called` evaluator can grade recall via `args_match.doc_ids`. The
retrieved IDs are sorted alphabetically before recording — `tool_called`
does exact list equality, so a stable order makes the eval YAML readable.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

_DOCS_PATH = Path(__file__).parent / "fixtures" / "docs.jsonl"
_TOP_K = 2
_LLM_MODEL = "claude-haiku-4-5-20251001"

_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "do", "for",
        "from", "get", "how", "i", "in", "is", "many", "of", "on", "or",
        "that", "the", "this", "to", "what", "who",
    }
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

_DOC_CACHE: list[dict[str, Any]] | None = None


def _tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS}


def _load_docs() -> list[dict[str, Any]]:
    global _DOC_CACHE
    if _DOC_CACHE is None:
        rows: list[dict[str, Any]] = []
        for raw in _DOCS_PATH.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line:
                rows.append(json.loads(line))
        _DOC_CACHE = rows
    return _DOC_CACHE


def _retrieve(question: str, k: int = _TOP_K) -> list[dict[str, Any]]:
    q_tokens = _tokens(question)
    scored: list[tuple[int, str, dict[str, Any]]] = []
    for doc in _load_docs():
        d_tokens = _tokens(doc["title"] + " " + doc["text"])
        score = len(q_tokens & d_tokens)
        # Sort key: descending score (so negate) then ascending id.
        scored.append((-score, doc["id"], doc))
    scored.sort()
    return [doc for _, _, doc in scored[:k]]


def _stub_answer(retrieved: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for doc in retrieved:
        first_sentence = doc["text"].split(". ", 1)[0].rstrip(".") + "."
        parts.append(first_sentence)
    return "Based on company policy: " + " ".join(parts)


async def _llm_answer(
    question: str, retrieved: list[dict[str, Any]]
) -> tuple[str, dict[str, int]]:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic()
    context = "\n\n".join(
        f"[{d['id']}] {d['title']}: {d['text']}" for d in retrieved
    )
    prompt = (
        "Use only the documents below to answer the question. If the answer "
        "is not in the documents, say so.\n\n"
        f"Documents:\n{context}\n\nQuestion: {question}\n\nAnswer:"
    )
    resp = await client.messages.create(
        model=_LLM_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    return text, {
        "token_input": resp.usage.input_tokens,
        "token_output": resp.usage.output_tokens,
    }


async def run(
    case: dict[str, Any], variant: dict[str, Any] | None = None
) -> dict[str, Any]:
    del variant  # single-variant example; no per-variant behavior.
    question = case["input"]["question"]

    retrieved = _retrieve(question, k=_TOP_K)
    doc_ids = sorted(d["id"] for d in retrieved)

    use_llm = (
        os.environ.get("EVALH_RAG_USE_LLM") == "1"
        and bool(os.environ.get("ANTHROPIC_API_KEY"))
    )
    metrics: dict[str, int] = {}
    if use_llm:
        answer, metrics = await _llm_answer(question, retrieved)
    else:
        answer = _stub_answer(retrieved)

    return {
        "final_answer": answer,
        "tool_calls": [
            {
                "name": "retriever",
                "arguments": {"query": question, "doc_ids": doc_ids},
            }
        ],
        "metrics": metrics,
    }
