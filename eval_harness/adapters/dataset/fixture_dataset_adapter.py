"""Fixture DatasetAdapter — the test surface for the embed_full_trace contract.

Reads a single YAML file with ``cases:`` (and optional ``embedded_traces:``)
and produces ``EvalCase`` s. With ``embed_full_trace: true``, each case's
``_embedded_trace`` is populated from the YAML so the ``replay``
SystemAdapter can unwrap it.

Real langfuse / phoenix / arize adapters land separately; this one keeps the
v1 online-eval pipeline runnable offline.

File shape — combined::

    schema_version: "1.0"
    cases:
      - id: case_001
        input: { user_message: "..." }
        embedded_trace:           # optional; required if embed_full_trace=true
          run_id: original_run
          case_id: case_001
          variant_name: production
          started_at: "2026-05-01T10:00:00Z"
          finished_at: "2026-05-01T10:00:03Z"
          latency_ms: 3000
          input: { user_message: "..." }
          output: { final_answer: "..." }
          metrics: { token_input: 100, token_output: 50, cost_usd: 0.012 }
          extra: { trace_id: "lf_abc123" }
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import EvalCase, ExpectedBehavior, Trace


class FixtureDatasetAdapter:
    # `embed_full_trace` is YAML-configurable here (unlike yaml/jsonl, which
    # always have it False as a class constant), so it lives on the instance.
    # The Protocol in base.py accepts either shape — mypy matches structurally.
    embed_full_trace: bool

    def __init__(
        self,
        path: str | Path,
        *,
        embed_full_trace: bool = False,
        **kwargs: Any,
    ) -> None:
        self.path = Path(path)
        self.embed_full_trace = embed_full_trace
        self._extra = kwargs

    async def load_cases(self) -> list[EvalCase]:
        try:
            raw = self.path.read_text()
        except OSError as e:
            raise AdapterError(f"Cannot read dataset file {self.path}: {e}") from e

        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as e:
            raise ConfigError(f"YAML parse error in {self.path}: {e}") from e

        if not isinstance(data, dict):
            raise ConfigError(
                f"{self.path}: top-level must be a mapping, got "
                f"{type(data).__name__}"
            )
        raw_cases = data.get("cases")
        if not isinstance(raw_cases, list):
            raise ConfigError(
                f"{self.path}: 'cases' must be a list, got "
                f"{type(raw_cases).__name__}"
            )

        cases: list[EvalCase] = []
        seen_ids: set[str] = set()
        for i, raw_case in enumerate(raw_cases):
            if not isinstance(raw_case, dict):
                raise ConfigError(
                    f"{self.path}: cases[{i}] must be a mapping"
                )
            case = self._parse_case(raw_case, i)
            if case.id in seen_ids:
                raise ConfigError(
                    f"{self.path}: duplicate case id '{case.id}' (cases[{i}])"
                )
            seen_ids.add(case.id)
            cases.append(case)
        return cases

    def _parse_case(self, raw: dict[str, Any], index: int) -> EvalCase:
        expected_raw = raw.get("expected")
        if expected_raw is None:
            expected = ExpectedBehavior()
        elif isinstance(expected_raw, dict):
            try:
                expected = ExpectedBehavior.model_validate(expected_raw)
            except ValidationError as e:
                raise ConfigError(
                    f"{self.path}: cases[{index}].expected invalid: {e}"
                ) from e
        else:
            raise ConfigError(
                f"{self.path}: cases[{index}].expected must be a mapping"
            )

        try:
            case = EvalCase(
                id=str(raw["id"]),
                input=dict(raw.get("input") or {}),
                metadata=dict(raw.get("metadata") or {}),
                expected=expected,
            )
        except (KeyError, ValidationError) as e:
            raise ConfigError(
                f"{self.path}: cases[{index}] invalid: {e}"
            ) from e

        if self.embed_full_trace:
            trace_raw = raw.get("embedded_trace")
            if trace_raw is None:
                raise ConfigError(
                    f"{self.path}: cases[{index}] missing 'embedded_trace' "
                    f"(adapter was built with embed_full_trace=true)"
                )
            if not isinstance(trace_raw, dict):
                raise ConfigError(
                    f"{self.path}: cases[{index}].embedded_trace must be a "
                    f"mapping"
                )
            try:
                trace = Trace.model_validate(trace_raw)
            except ValidationError as e:
                raise ConfigError(
                    f"{self.path}: cases[{index}].embedded_trace invalid: {e}"
                ) from e
            case._embedded_trace = trace

        return case
