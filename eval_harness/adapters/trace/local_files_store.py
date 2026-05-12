from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path
from types import TracebackType
from typing import Any, Self

import yaml

from eval_harness.core.errors import AdapterError
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
    def __init__(self, path: str | Path, **kwargs: Any) -> None:
        self.base_path = Path(path)
        self._extra = kwargs
        self.rendered_config: dict[str, Any] | None = None
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
        self._run_dir = run_dir
        await asyncio.to_thread(run_dir.mkdir, parents=True, exist_ok=True)

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

    def _require_run_dir(self) -> Path:
        if self._run_dir is None:
            raise AdapterError("LocalFilesStore used before open() was called")
        return self._run_dir


def _append_text(path: Path, text: str) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(text)
