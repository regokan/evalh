"""Replay SystemAdapter — online evaluation.

Does NOT invoke any system. Unwraps ``case._embedded_trace`` (set by a
DatasetAdapter with ``embed_full_trace=true``) and returns it with replay
provenance attached.

CRITICAL invariant: the returned trace preserves the original
``started_at``, ``finished_at``, ``latency_ms``, and every metric byte-for-
byte. The runner cooperates by skipping its usual latency-overwrite step
when ``trace.extra["source"] == "replay"`` (see
``runner/run_eval.py::_enforce_invariants``).

See docs/Adapters.md > "v1: replay" and docs/Observability.md > Pattern 4.
"""

from __future__ import annotations

from types import TracebackType
from typing import Any, Self

from eval_harness.adapters.workspace.base import Workspace
from eval_harness.core.errors import AdapterError
from eval_harness.core.models import EvalCase, RunVariant, Trace
from eval_harness.core.time import utc_now


class ReplaySystemAdapter:
    name: str

    def __init__(
        self,
        name: str = "replay",
        *,
        metadata: dict[str, Any] | None = None,
        **_extra: Any,
    ) -> None:
        self.name = name
        self._metadata = dict(metadata or {})

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def run(
        self,
        case: EvalCase,
        variant: RunVariant,
        workspace: Workspace | None,
    ) -> Trace:
        embedded = case._embedded_trace
        if embedded is None:
            raise AdapterError(
                f"replay adapter '{self.name}': case '{case.id}' has no "
                f"_embedded_trace; the DatasetAdapter must set "
                f"embed_full_trace=true (e.g. langfuse, phoenix, arize, "
                f"fixture)."
            )

        # Deep-copy so we never mutate the dataset adapter's cached trace.
        # Pydantic's model_copy(deep=True) handles nested models + dicts.
        clone = embedded.model_copy(deep=True)

        # Set the join keys the runner / trace store / evaluators all rely on
        # to current values. Per docs/Adapters.md > "v1: replay" these are the
        # only fields a replay adapter mutates besides `extra`.
        clone.case_id = case.id
        clone.variant_name = variant.name
        # run_id intentionally not set here — the runner's _enforce_invariants
        # owns it (the runner is the source of truth for the current run_id).

        # Attach replay provenance. Preserve any existing extra keys (e.g. an
        # upstream `trace_id` the dataset adapter recorded).
        existing_trace_id = clone.extra.get("trace_id")
        platform = self._metadata.get("source") or clone.extra.get("source_platform")
        clone.extra["source"] = "replay"
        clone.extra["replayed_from"] = {
            "platform": platform,
            "trace_id": existing_trace_id,
            "fetched_at": utc_now().isoformat(),
        }
        return clone
