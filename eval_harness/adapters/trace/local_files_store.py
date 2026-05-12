from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import yaml
from pydantic import ValidationError

from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import (
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    Trace,
)

_MASK = "***MASKED***"
_SECRET_KEY_RE = re.compile(
    r"(.*_KEY|.*_TOKEN|.*_SECRET|password|api_key)$", re.IGNORECASE
)


def mask_secrets(config: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``config`` with secret-named fields masked.

    A field name matches if it equals (case-insensitive) ``password`` or
    ``api_key``, or ends in ``_KEY`` / ``_TOKEN`` / ``_SECRET``. Matches anywhere
    in the tree, including inside lists.
    """
    return _mask_value(config)  # type: ignore[no-any-return]


def _mask_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: (_MASK if isinstance(k, str) and _SECRET_KEY_RE.fullmatch(k) else _mask_value(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_mask_value(item) for item in value]
    return value


class LocalFilesStore:
    # Uses asyncio.to_thread for blocking file appends rather than adding an
    # aiofiles dependency. JSONL appends are short, infrequent (one per
    # case x variant), and an asyncio.Lock serializes writes.
    def __init__(
        self,
        path: str | Path,
        *,
        run_namespace: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> None:
        self.base_path = Path(path)
        # ``run_namespace`` is accepted for cross-backend config parity but is
        # ignored by this single-tenant store — the on-disk layout remains
        # flat ``runs/<run_id>/``. Multi-tenant isolation is the postgres
        # store's job. See docs/DataModel.md > Run namespacing.
        self.run_namespace = dict(run_namespace) if run_namespace else None
        self._extra = kwargs
        self.rendered_config: dict[str, Any] | None = None
        self._run_id: str | None = None
        self._run_dir: Path | None = None
        self._traces_lock = asyncio.Lock()
        self._results_lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    async def open(self, run_id: str, run_dir: Path) -> None:
        self._run_id = run_id
        self._run_dir = run_dir
        await asyncio.to_thread(run_dir.mkdir, parents=True, exist_ok=True)
        # Pre-seed the append-only JSONL files so list_run_ids sees this run
        # even before any cell finishes. config.yaml stays opt-in (written
        # only when rendered_config is set), summary.yaml is written by
        # save_summary on completion.
        for sentinel in ("traces.jsonl", "results.jsonl"):
            await asyncio.to_thread(_touch, run_dir / sentinel)

        if self.rendered_config is not None:
            masked = mask_secrets(self.rendered_config)
            masked_yaml = yaml.safe_dump(masked, sort_keys=False)
            await asyncio.to_thread(
                (run_dir / "config.yaml").write_text, masked_yaml
            )
            unmasked_yaml = yaml.safe_dump(self.rendered_config, sort_keys=False)
            config_hash = hashlib.sha256(unmasked_yaml.encode("utf-8")).hexdigest()
            await asyncio.to_thread(
                (run_dir / "config_hash.txt").write_text, config_hash + "\n"
            )

    async def save_trace(self, trace: Trace) -> None:
        run_dir = self._require_run_dir()
        line = trace.model_dump_json() + "\n"
        async with self._traces_lock:
            await asyncio.to_thread(_append_text, run_dir / "traces.jsonl", line)

    async def save_trace_idempotent(self, trace: Trace, cell_id: str) -> bool:
        """v2 idempotency: per-cell sidecar marker at
        ``runs/<id>/cells/<cell_id>.success.marker``. A successful
        save writes the marker; a subsequent call sees the marker and
        no-ops. A failed cell (trace.error != None) skips the marker so
        a retry overwrites the previous error trace."""
        run_dir = self._require_run_dir()
        cells_dir = run_dir / "cells"
        marker = cells_dir / f"{cell_id}.success.marker"
        async with self._traces_lock:
            if await asyncio.to_thread(marker.exists):
                return False
            await asyncio.to_thread(cells_dir.mkdir, parents=True, exist_ok=True)
            line = trace.model_dump_json() + "\n"
            await asyncio.to_thread(_append_text, run_dir / "traces.jsonl", line)
            if trace.error is None:
                await asyncio.to_thread(marker.write_text, cell_id + "\n")
        return True

    async def save_evaluation(
        self,
        case_id: str,
        variant: str,
        results: list[EvaluationResult],
    ) -> None:
        if not results:
            return
        run_dir = self._require_run_dir()
        payload = "".join(r.model_dump_json() + "\n" for r in results)
        async with self._results_lock:
            await asyncio.to_thread(_append_text, run_dir / "results.jsonl", payload)

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        run_dir = self._require_run_dir()
        artifact_dir = (
            run_dir / "artifacts" / artifact.case_id / artifact.variant_name
        )
        await asyncio.to_thread(artifact_dir.mkdir, parents=True, exist_ok=True)
        body = artifact.model_dump_json(indent=2)
        await asyncio.to_thread(
            (artifact_dir / "artifact.json").write_text, body
        )

    async def save_summary(self, summary: RunSummary) -> None:
        run_dir = self._require_run_dir()
        data = summary.model_dump(mode="json")
        body = yaml.safe_dump(data, sort_keys=False)
        await asyncio.to_thread((run_dir / "summary.yaml").write_text, body)

    async def close(self) -> None:
        return None

    # ---- Read methods (v0.2) ----

    async def list_run_ids(self) -> list[str]:
        if not await asyncio.to_thread(self.base_path.is_dir):
            return []
        entries = await asyncio.to_thread(_scan_run_dirs, self.base_path)
        return sorted(entries)

    async def load_summary(self, run_id: str) -> RunSummary | None:
        summary_path = self.base_path / run_id / "summary.yaml"
        if not await asyncio.to_thread(summary_path.is_file):
            return None
        text = await asyncio.to_thread(summary_path.read_text)
        data = yaml.safe_load(text)
        try:
            return RunSummary.model_validate(data)
        except ValidationError as e:
            raise ConfigError(
                f"local_files: summary.yaml in {self.base_path / run_id} "
                f"failed validation: {e}"
            ) from e

    async def iter_traces(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[Trace]:
        run_ids = [run_id] if run_id is not None else await self.list_run_ids()
        for rid in run_ids:
            async for trace in self._iter_jsonl(
                self.base_path / rid / "traces.jsonl", Trace
            ):
                yield trace

    async def iter_results(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[EvaluationResult]:
        run_ids = [run_id] if run_id is not None else await self.list_run_ids()
        for rid in run_ids:
            async for result in self._iter_jsonl(
                self.base_path / rid / "results.jsonl", EvaluationResult
            ):
                yield result

    async def _iter_jsonl(
        self, path: Path, model: type[Any]
    ) -> AsyncIterator[Any]:
        if not await asyncio.to_thread(path.is_file):
            return
        text = await asyncio.to_thread(path.read_text)
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if not raw.strip():
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as e:
                raise ConfigError(f"{path.name}:{lineno}: invalid JSON: {e}") from e
            try:
                yield model.model_validate(data)
            except ValidationError as e:
                raise ConfigError(
                    f"{path.name}:{lineno}: schema validation failed: {e}"
                ) from e

    def _require_run_dir(self) -> Path:
        if self._run_dir is None:
            raise AdapterError("LocalFilesStore used before open() was called")
        return self._run_dir


# A directory counts as a run when both append-only files exist; config.yaml
# and summary.yaml are written later in the lifecycle.
_RUN_MARKERS = ("traces.jsonl", "results.jsonl")


def _scan_run_dirs(base: Path) -> list[str]:
    out: list[str] = []
    for child in base.iterdir():
        if not child.is_dir():
            continue
        if all((child / name).is_file() for name in _RUN_MARKERS):
            out.append(child.name)
    return out


def _append_text(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text)


def _touch(path: Path) -> None:
    if not path.exists():
        path.touch()
