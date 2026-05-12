"""Braintrust TraceStore — push our traces upward as experiment log entries.

When listed as a non-first sink in ``eval.yaml > output:``, this store's
failures are caught by the runner and logged into ``RunSummary.sink_errors``
(per ev-7aj). The store itself just raises cleanly on push failures —
canonical/secondary asymmetry lives in the runner, not here.

Auth + SDK plumbing live in
`eval_harness._platforms.braintrust.BraintrustClient`.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Self

from eval_harness._platforms.braintrust import (
    BraintrustClient,
    get_or_create_braintrust_client,
    release_braintrust_client,
)
from eval_harness.core.errors import AdapterError
from eval_harness.core.models import (
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    Trace,
)


class BraintrustTraceStore:
    """Push-side TraceStore for Braintrust. Implements the TraceStore
    Protocol so it slots into ``output:[...]`` interchangeably with other
    stores.

    Mapping:

    - ``save_trace``      -> ``client.push_trace({kind: "trace", ...})``;
      one Braintrust log entry per cell.
    - ``save_evaluation`` -> one ``kind: "scores"`` payload carrying every
      EvaluationResult for a (case, variant) pair; the SDK shim decides
      whether to split into multiple network calls.
    - ``save_artifact`` and ``save_summary`` are no-ops — Braintrust
      doesn't model filesystem diffs, and the summary stays canonical on
      ``runs/<run_id>/summary.yaml``.
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        project: str | None = None,
        org: str | None = None,
        client: BraintrustClient | None = None,
        **_extra: Any,
    ) -> None:
        self._owns_client = client is None
        self._client: BraintrustClient = client or get_or_create_braintrust_client(
            api_key=api_key, project=project, org=org
        )
        self._run_id: str = ""
        self.rendered_config: dict[str, Any] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await self._client.flush()
        finally:
            if self._owns_client:
                release_braintrust_client(self._client)

    async def open(self, run_id: str, run_dir: Path) -> None:
        self._run_id = run_id

    async def save_trace(self, trace: Trace) -> None:
        if not self._run_id:
            raise AdapterError("BraintrustTraceStore: save_trace before open()")
        payload = _trace_to_braintrust(trace, run_id=self._run_id)
        await self._client.push_trace(payload)

    async def save_trace_idempotent(self, trace: Trace, cell_id: str) -> bool:
        # Idempotency lives on the canonical sink — Braintrust always-writes.
        await self.save_trace(trace)
        return True

    async def save_evaluation(
        self,
        case_id: str,
        variant: str,
        results: list[EvaluationResult],
    ) -> None:
        if not results:
            return
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
        # Braintrust doesn't model filesystem diffs. The canonical sink
        # keeps the artifact on disk; we intentionally do nothing here.
        return None

    async def save_summary(self, summary: RunSummary) -> None:
        # Summary belongs on the canonical sink. Mirroring it into
        # Braintrust would duplicate data without adding signal.
        return None


def _trace_to_braintrust(trace: Trace, *, run_id: str) -> dict[str, Any]:
    """Translate our Trace into a flat Braintrust log payload."""
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
