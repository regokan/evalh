"""Coding-agent stub for the Eval Harness coding_agent example.

The agent receives a case describing a TODO-style coding task, reads the
working copy of `fixture_repo/` (which the `tempdir_snapshot` workspace
has staged for this run), asks Claude for a single-file patch, writes it
back to the workspace, and returns a Trace.

The `command` evaluator then runs `pytest` in the artifact dir to grade
whether the agent's edit actually made the tests pass.

Requires `ANTHROPIC_API_KEY`. Picked up from either the shell environment
or `examples/coding_agent/.env` (gitignored).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from anthropic import AsyncAnthropic
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=False)

_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 1024

_SYSTEM_PROMPT = (
    "You edit a single Python file to make failing tests pass. You receive "
    "the full source of one file and a brief description of the task. "
    "Reply with a JSON object of the form "
    '{"path": "<relative path>", "content": "<full new file contents>"}. '
    "Do not include any prose outside the JSON. The new content must be the "
    "entire file, ready to write as-is."
)


async def run(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    workspace_path = variant.get("_workspace_path")
    if not workspace_path:
        return _failure("python_function adapter did not provide _workspace_path")
    repo = Path(workspace_path)

    target_rel = case["input"]["target_file"]
    task = case["input"]["task"]
    target = repo / target_rel
    if not target.is_file():
        return _failure(f"target file not found in workspace: {target_rel}")

    source = target.read_text()
    prompt = (
        f"Task: {task}\n\n"
        f"File: {target_rel}\n"
        f"```python\n{source}\n```\n\n"
        "Return ONLY the JSON object."
    )

    client = AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = await client.messages.create(
        model=_MODEL,
        max_tokens=_MAX_TOKENS,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    parsed = _extract_json(text)
    if parsed is None or "content" not in parsed:
        return _failure(
            f"agent did not return parseable JSON; raw: {text[:200]!r}",
            tokens=(resp.usage.input_tokens, resp.usage.output_tokens),
        )

    written_rel = parsed.get("path", target_rel)
    written = repo / written_rel
    if not written.resolve().is_relative_to(repo.resolve()):
        return _failure(f"agent tried to write outside workspace: {written_rel}")
    written.write_text(parsed["content"])

    return {
        "final_answer": f"Edited {written_rel} ({len(parsed['content'])} bytes).",
        "metrics": {
            "token_input": resp.usage.input_tokens,
            "token_output": resp.usage.output_tokens,
        },
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    # Strip a possible ```json fence the model adds despite instructions.
    if text.startswith("```"):
        text = text.strip("`")
        text = text.removeprefix("json").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        out = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


def _failure(
    message: str, *, tokens: tuple[int, int] = (0, 0)
) -> dict[str, Any]:
    return {
        "final_answer": f"(agent failed) {message}",
        "metrics": {"token_input": tokens[0], "token_output": tokens[1]},
    }
