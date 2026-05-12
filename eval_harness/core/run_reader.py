from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import EvaluationResult, RunSummary, Trace

_REQUIRED_FILES = ("config.yaml", "traces.jsonl", "results.jsonl", "summary.yaml")


class RunReader:
    """Streaming reader for a single ``runs/<run_id>/`` directory.

    The three v0.1 CLI commands (re-evaluate, inspect, compare) and any future
    tooling go through this class so JSONL parsing + Pydantic deserialization
    lives in one place. Constructor validates the directory layout; all reads
    are async.
    """

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        if not run_dir.is_dir():
            raise ConfigError(f"run directory does not exist: {run_dir}")
        for name in _REQUIRED_FILES:
            if not (run_dir / name).is_file():
                raise ConfigError(f"missing required file in run dir: {run_dir / name}")

    async def load_summary(self) -> RunSummary:
        text = await asyncio.to_thread((self.run_dir / "summary.yaml").read_text)
        data = yaml.safe_load(text)
        try:
            return RunSummary.model_validate(data)
        except ValidationError as e:
            raise ConfigError(f"summary.yaml in {self.run_dir} failed validation: {e}") from e

    async def load_config(self) -> dict[str, Any]:
        text = await asyncio.to_thread((self.run_dir / "config.yaml").read_text)
        loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise ConfigError(
                f"config.yaml in {self.run_dir} must be a mapping, got {type(loaded).__name__}"
            )
        return loaded

    async def iter_traces(self) -> AsyncIterator[Trace]:
        async for raw in self._iter_jsonl("traces.jsonl"):
            yield self._parse_line(raw, Trace, "traces.jsonl")

    async def iter_results(self) -> AsyncIterator[EvaluationResult]:
        async for raw in self._iter_jsonl("results.jsonl"):
            yield self._parse_line(raw, EvaluationResult, "results.jsonl")

    async def get_trace(self, case_id: str, variant_name: str) -> Trace | None:
        async for trace in self.iter_traces():
            if trace.case_id == case_id and trace.variant_name == variant_name:
                return trace
        return None

    async def get_results(
        self, case_id: str, variant_name: str
    ) -> list[EvaluationResult]:
        out: list[EvaluationResult] = []
        async for result in self.iter_results():
            if result.case_id == case_id and result.variant_name == variant_name:
                out.append(result)
        return out

    async def list_case_ids(self) -> list[str]:
        seen: list[str] = []
        seen_set: set[str] = set()
        async for trace in self.iter_traces():
            if trace.case_id not in seen_set:
                seen.append(trace.case_id)
                seen_set.add(trace.case_id)
        return seen

    async def list_variant_names(self) -> list[str]:
        seen: list[str] = []
        seen_set: set[str] = set()
        async for trace in self.iter_traces():
            if trace.variant_name not in seen_set:
                seen.append(trace.variant_name)
                seen_set.add(trace.variant_name)
        return seen

    async def _iter_jsonl(self, filename: str) -> AsyncIterator[tuple[int, str]]:
        path = self.run_dir / filename
        text = await asyncio.to_thread(path.read_text)
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if not raw.strip():
                continue
            yield lineno, raw

    @staticmethod
    def _parse_line(raw: tuple[int, str], model: type[Any], filename: str) -> Any:
        lineno, text = raw
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{filename}:{lineno}: invalid JSON: {e}") from e
        try:
            return model.model_validate(data)
        except ValidationError as e:
            raise ConfigError(
                f"{filename}:{lineno}: schema validation failed: {e}"
            ) from e
