"""Pod entrypoint for the K8s Jobs executor.

Each Kubernetes Job runs this script as its container command. It reads
the serialised ``CellDescriptor`` from the pod environment (or a
ConfigMap-mounted file when the payload is too large for env vars),
runs the cell through ``worker_run_cell_sync`` — which rebuilds adapters
and evaluators from the entry-point registry — and writes the outcome
JSON back through the configured ObjectStorage so the orchestrator can
read it.

Env vars the orchestrator sets on the pod:

- ``EVALH_CELL_PAYLOAD``      — JSON-encoded cell dict, used when ≤ 32 KB.
- ``EVALH_CELL_PAYLOAD_PATH`` — path to the ConfigMap-mounted JSON,
                                used when the payload would exceed env
                                size limits (~1 MB hard cap; we switch
                                at 32 KB to leave headroom).
- ``EVALH_STORAGE_URL``       — fsspec URL where the outcome is written.
- ``EVALH_STORAGE_CREDENTIALS_JSON`` — optional JSON credentials dict.
- ``EVALH_OUTCOME_KEY``       — relative key under ``EVALH_STORAGE_URL``.
- ``EVALH_TIMEOUT_SECONDS``   — optional per-cell timeout (float).

Console-script entry: ``evalh-cell-worker``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

from eval_harness.core.executors._worker import worker_run_cell
from eval_harness.core.object_storage.fsspec_storage import FsspecObjectStorage


def _load_payload() -> dict[str, Any]:
    """Pull the cell payload from env (small) or a mounted file (large)."""
    inline = os.environ.get("EVALH_CELL_PAYLOAD")
    if inline:
        loaded: dict[str, Any] = json.loads(inline)
        return loaded
    path = os.environ.get("EVALH_CELL_PAYLOAD_PATH")
    if not path:
        raise RuntimeError(
            "evalh-cell-worker: neither EVALH_CELL_PAYLOAD nor "
            "EVALH_CELL_PAYLOAD_PATH is set"
        )
    with open(path, encoding="utf-8") as f:
        loaded = json.load(f)
    return loaded


def _load_timeout() -> float | None:
    raw = os.environ.get("EVALH_TIMEOUT_SECONDS")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


async def _amain() -> int:
    payload = _load_payload()
    storage_url = os.environ.get("EVALH_STORAGE_URL")
    outcome_key = os.environ.get("EVALH_OUTCOME_KEY")
    if not storage_url or not outcome_key:
        raise RuntimeError(
            "evalh-cell-worker: EVALH_STORAGE_URL and EVALH_OUTCOME_KEY "
            "must both be set"
        )
    creds_json = os.environ.get("EVALH_STORAGE_CREDENTIALS_JSON")
    credentials = json.loads(creds_json) if creds_json else None

    outcome = await worker_run_cell(payload, timeout_seconds=_load_timeout())
    body = json.dumps(outcome).encode("utf-8")
    async with FsspecObjectStorage(url=storage_url, credentials=credentials) as storage:
        await storage.put(outcome_key, body)
    return 0


def main() -> int:
    """Synchronous shim — ``console_scripts`` entry points are sync."""
    return asyncio.run(_amain())


if __name__ == "__main__":  # pragma: no cover — exercised through console_scripts
    sys.exit(main())


__all__ = ["main"]
