"""SQL-equivalence evaluator. Compares the candidate SQL in
`trace.output.final_answer` against a `reference_sql` from config by running
both against an in-memory sqlite DB seeded by `setup_sql`. The case passes
when both queries return the same rowset (order-insensitive unless
`order_matters=true`).
"""

from __future__ import annotations

import sqlite3
import traceback
from typing import Any, ClassVar

from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    Trace,
    TraceError,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.base import Evaluator


class SqlEquivalentEvaluator(Evaluator):
    type: ClassVar[str] = "sql_equivalent"

    @classmethod
    def validate_config(cls, config: dict[str, Any]) -> None:
        ref = config.get("reference_sql")
        if not isinstance(ref, str) or not ref.strip():
            raise ConfigError(
                "sql_equivalent: 'reference_sql' (non-empty string) is required"
            )
        setup = config.get("setup_sql", [])
        if not isinstance(setup, list) or not all(isinstance(s, str) for s in setup):
            raise ConfigError("sql_equivalent: 'setup_sql' must be list[str]")
        if "order_matters" in config and not isinstance(config["order_matters"], bool):
            raise ConfigError("sql_equivalent: 'order_matters' must be bool")

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        started = utc_now()
        candidate = trace.output.final_answer or ""
        reference_sql: str = self._config["reference_sql"]
        setup_sql: list[str] = list(self._config.get("setup_sql", []))
        order_matters: bool = bool(self._config.get("order_matters", False))

        try:
            candidate_rows = _run_query(candidate, setup_sql)
            reference_rows = _run_query(reference_sql, setup_sql)
        except sqlite3.Error as e:
            finished = utc_now()
            return EvaluationResult(
                run_id=trace.run_id,
                case_id=case.id,
                variant_name=trace.variant_name,
                evaluator=self.name,
                evaluator_type=self.type,
                passed=False,
                reason=f"sqlite error: {e}",
                started_at=started,
                finished_at=finished,
                latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
                error=TraceError(
                    type="adapter_error",
                    message=f"sqlite error: {e}",
                    stack=traceback.format_exc(),
                ),
            )

        if order_matters:
            passed = candidate_rows == reference_rows
        else:
            passed = sorted(candidate_rows) == sorted(reference_rows)

        finished = utc_now()
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=passed,
            score=1.0 if passed else 0.0,
            reason=(
                "rowsets match"
                if passed
                else f"rowsets differ: candidate={len(candidate_rows)} rows, "
                f"reference={len(reference_rows)} rows"
            ),
            detail={
                "candidate_rows": candidate_rows[:50],
                "reference_rows": reference_rows[:50],
                "order_matters": order_matters,
            },
            started_at=started,
            finished_at=finished,
            latency_ms=max(0, int((finished - started).total_seconds() * 1000)),
        )


def _run_query(sql: str, setup: list[str]) -> list[tuple[Any, ...]]:
    conn = sqlite3.connect(":memory:")
    try:
        for stmt in setup:
            conn.executescript(stmt)
        cur = conn.execute(sql)
        return [tuple(row) for row in cur.fetchall()]
    finally:
        conn.close()
