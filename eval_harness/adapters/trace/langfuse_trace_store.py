"""Langfuse TraceStore — push our traces upward as Langfuse observations.

When listed as a non-first sink in ``eval.yaml > output:``, this store's
failures are caught by the runner and logged into ``RunSummary.sink_errors``
(per ev-7aj). The store itself just raises cleanly on push failures —
canonical/secondary asymmetry lives in the runner, not here.

Auth + SDK plumbing live in `eval_harness._platforms.langfuse.LangfuseClient`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from eval_harness._platforms.langfuse import (
    LangfuseClient,
    get_or_create_langfuse_client,
    release_langfuse_client,
)
from eval_harness.core.errors import AdapterError
from eval_harness.core.models import (
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    Trace,
)

_log = logging.getLogger(__name__)


class LangfuseTraceStore:
    """Push-side TraceStore for Langfuse. Implements the same Protocol as
    `LocalFilesStore` so it slots into ``output:[...]`` interchangeably.

    Three save methods map to native Langfuse concepts:

    - ``save_trace``  -> create a Langfuse trace (one per eval case-cell)
    - ``save_evaluation`` -> create one Langfuse score per EvaluationResult,
      attached to the same trace_id; pass/fail in the score value.
    - ``save_artifact``   -> no-op (Langfuse doesn't model filesystem diffs)
    - ``save_summary``    -> no-op (summary lives canonically in
      ``runs/<run_id>/summary.yaml``; Langfuse isn't a summary store).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        host: str | None = None,
        client: LangfuseClient | None = None,
        **_extra: Any,
    ) -> None:
        # When invoked as a secondary sink in a multi-sink output, an empty
        # api_key (e.g. from `${LANGFUSE_API_KEY:-}` expansion when the env var
        # is unset) flips the store into a no-op. This keeps examples that
        # ship a langfuse sink runnable offline without the [langfuse] extra
        # installed — the SDK import is skipped entirely in disabled mode.
        self._disabled = client is None and not api_key
        self._owns_client = client is None and not self._disabled
        self._client: LangfuseClient | None
        if self._disabled:
            self._client = None
        else:
            self._client = client or get_or_create_langfuse_client(
                api_key=api_key, host=host
            )
        self._run_id: str = ""
        self._warned_disabled = False
        self.rendered_config: dict[str, Any] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._disabled or self._client is None:
            return
        # Best-effort drain so writes leave our process before the run's
        # AsyncExitStack tears down.
        try:
            await self._client.flush()
        finally:
            if self._owns_client:
                release_langfuse_client(self._client)

    async def open(self, run_id: str, run_dir: Path) -> None:
        self._run_id = run_id
        if self._disabled and not self._warned_disabled:
            _log.warning(
                "LangfuseTraceStore: LANGFUSE_API_KEY is unset; the langfuse "
                "sink will no-op for run %s. Set LANGFUSE_API_KEY (and "
                "optionally LANGFUSE_HOST) to mirror traces to Langfuse.",
                run_id,
            )
            self._warned_disabled = True

    async def save_trace(self, trace: Trace) -> None:
        if self._disabled:
            return
        if not self._run_id:
            raise AdapterError("LangfuseTraceStore: save_trace before open()")
        assert self._client is not None
        payload = _trace_to_langfuse(trace, run_id=self._run_id)
        await self._client.push_trace(payload)

    async def save_trace_idempotent(self, trace: Trace, cell_id: str) -> bool:
        # Idempotency lives on the canonical sink (local_files / sqlite /
        # postgres). Langfuse always-writes; duplicates are deduped on
        # the Langfuse side if the user configures it.
        if self._disabled:
            return True
        await self.save_trace(trace)
        return True

    async def save_evaluation(
        self,
        case_id: str,
        variant: str,
        results: list[EvaluationResult],
    ) -> None:
        if self._disabled or not results:
            return
        assert self._client is not None
        # Each EvaluationResult becomes one Langfuse score row. We pack them
        # into a single push_trace call so the SDK shim can decide whether to
        # split into multiple network requests (the production SDK batches).
        payload = {
            "kind": "scores",
            "run_id": self._run_id,
            "case_id": case_id,
            "variant_name": variant,
            "scores": [
                {
                    "evaluator": r.evaluator,
                    "evaluator_type": r.evaluator_type,
                    "passed": r.passed,
                    "score": r.score,
                    "reason": r.reason,
                }
                for r in results
            ],
        }
        await self._client.push_trace(payload)

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        # Langfuse doesn't model filesystem diffs natively. The canonical
        # sink (typically local_files) keeps the artifact on disk; we
        # intentionally do nothing here.
        return None

    async def save_summary(self, summary: RunSummary) -> None:
        # Summary belongs on the canonical sink. Mirroring it into Langfuse
        # would duplicate data without adding signal.
        return None


def _trace_to_langfuse(trace: Trace, *, run_id: str) -> dict[str, Any]:
    """Translate our Trace into a flat Langfuse-trace payload. Keep the SDK
    schema-mapping in one place so a Langfuse-SDK shape change touches one
    file."""
    return {
        "kind": "trace",
        "id": trace.extra.get("trace_id") or _synth_id(trace, run_id),
        "name": f"{run_id}/{trace.case_id}/{trace.variant_name}",
        "run_id": run_id,
        "case_id": trace.case_id,
        "variant_name": trace.variant_name,
        "started_at": trace.started_at.isoformat(),
        "finished_at": trace.finished_at.isoformat(),
        "latency_ms": trace.latency_ms,
        "input": dict(trace.input),
        "output": trace.output.model_dump(mode="json"),
        "metrics": trace.metrics.model_dump(mode="json", exclude_none=True),
        "messages": [m.model_dump(mode="json") for m in trace.messages],
        "tool_calls": [t.model_dump(mode="json") for t in trace.tool_calls],
        "tool_results": [t.model_dump(mode="json") for t in trace.tool_results],
        "error": trace.error.model_dump(mode="json") if trace.error else None,
        "metadata": {**trace.extra, "run_id": run_id},
    }


def _synth_id(trace: Trace, run_id: str) -> str:
    return f"{run_id}__{trace.case_id}__{trace.variant_name}"
