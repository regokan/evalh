"""Postgres trace store. Optional install — `pip install 'eval-harness[postgres]'`.

Backed by asyncpg with JSONB payloads and server-side cursors for streaming
reads. run_namespace lives in a JSONB column so multi-tenant queries via
jsonb_path operators are cheap.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self

from eval_harness.core.errors import AdapterError, ConfigError
from eval_harness.core.models import (
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    Trace,
)

if TYPE_CHECKING:
    import asyncpg


_TABLES_SQL = """
CREATE TABLE IF NOT EXISTS {schema}.eval_runs (
    run_id        TEXT PRIMARY KEY,
    namespace     JSONB,
    started_at    TIMESTAMPTZ,
    finished_at   TIMESTAMPTZ,
    config_yaml   TEXT,
    summary_yaml  TEXT
);

CREATE TABLE IF NOT EXISTS {schema}.eval_traces (
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    variant_name  TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    namespace     JSONB,
    payload       JSONB NOT NULL,
    PRIMARY KEY (run_id, case_id, variant_name)
);

CREATE TABLE IF NOT EXISTS {schema}.eval_results (
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    variant_name  TEXT NOT NULL,
    evaluator     TEXT NOT NULL,
    started_at    TIMESTAMPTZ NOT NULL,
    namespace     JSONB,
    payload       JSONB NOT NULL,
    PRIMARY KEY (run_id, case_id, variant_name, evaluator)
);

CREATE TABLE IF NOT EXISTS {schema}.eval_artifacts (
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    variant_name  TEXT NOT NULL,
    namespace     JSONB,
    payload       JSONB NOT NULL,
    PRIMARY KEY (run_id, case_id, variant_name)
);
"""


class PostgresStore:
    """TraceStore backed by Postgres via asyncpg.

    Streams reads via ``connection.cursor()`` (server-side cursor) so
    100K-case runs don't materialise in memory. ``run_namespace`` is stored
    as JSONB on every row to make multi-tenant filtering a single index
    lookup.
    """

    def __init__(
        self,
        dsn: str | None = None,
        *,
        schema: str = "public",
        pool_size: int = 5,
        run_namespace: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> None:
        if not dsn or not isinstance(dsn, str):
            raise ConfigError("postgres trace store: 'dsn' (string) is required")
        if not _is_safe_identifier(schema):
            raise ConfigError(
                f"postgres trace store: 'schema' must be a simple identifier, "
                f"got {schema!r}"
            )

        try:
            import asyncpg as _asyncpg
        except ImportError as e:
            raise ConfigError(
                "postgres trace store requested but `asyncpg` is not installed. "
                "Install with: pip install 'eval-harness[postgres]'"
            ) from e

        self._asyncpg = _asyncpg
        self.dsn = dsn
        self.schema = schema
        self.pool_size = pool_size
        self.run_namespace = dict(run_namespace) if run_namespace else None
        self._namespace_blob = (
            json.dumps(self.run_namespace, sort_keys=True)
            if self.run_namespace is not None
            else None
        )
        self._run_id: str | None = None
        self._pool: asyncpg.Pool | None = None

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
        try:
            self._pool = await self._asyncpg.create_pool(
                self.dsn, min_size=1, max_size=self.pool_size
            )
        except Exception as e:
            raise ConfigError(
                f"postgres trace store: could not connect to {self.dsn!r}: {e}"
            ) from e
        async with self._pool.acquire() as conn:
            await conn.execute(_TABLES_SQL.format(schema=self.schema))
            await conn.execute(
                f"INSERT INTO {self.schema}.eval_runs (run_id, namespace) "
                f"VALUES ($1, $2::jsonb) ON CONFLICT (run_id) DO NOTHING",
                run_id,
                self._namespace_blob,
            )

    async def save_trace(self, trace: Trace) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self.schema}.eval_traces "
                f"(run_id, case_id, variant_name, started_at, namespace, payload) "
                f"VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb) "
                f"ON CONFLICT (run_id, case_id, variant_name) "
                f"DO UPDATE SET started_at = EXCLUDED.started_at, "
                f"namespace = EXCLUDED.namespace, payload = EXCLUDED.payload",
                trace.run_id,
                trace.case_id,
                trace.variant_name,
                trace.started_at,
                self._namespace_blob,
                trace.model_dump_json(),
            )

    async def save_evaluation(
        self,
        case_id: str,
        variant: str,
        results: list[EvaluationResult],
    ) -> None:
        if not results:
            return
        pool = self._require_pool()
        rows = [
            (
                r.run_id,
                r.case_id,
                r.variant_name,
                r.evaluator,
                r.started_at,
                self._namespace_blob,
                r.model_dump_json(),
            )
            for r in results
        ]
        async with pool.acquire() as conn:
            await conn.executemany(
                f"INSERT INTO {self.schema}.eval_results "
                f"(run_id, case_id, variant_name, evaluator, started_at, "
                f"namespace, payload) "
                f"VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb) "
                f"ON CONFLICT (run_id, case_id, variant_name, evaluator) "
                f"DO UPDATE SET started_at = EXCLUDED.started_at, "
                f"namespace = EXCLUDED.namespace, payload = EXCLUDED.payload",
                rows,
            )

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        pool = self._require_pool()
        run_id = self._run_id or ""
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self.schema}.eval_artifacts "
                f"(run_id, case_id, variant_name, namespace, payload) "
                f"VALUES ($1, $2, $3, $4::jsonb, $5::jsonb) "
                f"ON CONFLICT (run_id, case_id, variant_name) "
                f"DO UPDATE SET namespace = EXCLUDED.namespace, "
                f"payload = EXCLUDED.payload",
                run_id,
                artifact.case_id,
                artifact.variant_name,
                self._namespace_blob,
                artifact.model_dump_json(),
            )

    async def save_summary(self, summary: RunSummary) -> None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                f"INSERT INTO {self.schema}.eval_runs "
                f"(run_id, namespace, started_at, finished_at, summary_yaml) "
                f"VALUES ($1, $2::jsonb, $3, $4, $5) "
                f"ON CONFLICT (run_id) DO UPDATE SET "
                f"namespace = EXCLUDED.namespace, "
                f"started_at = EXCLUDED.started_at, "
                f"finished_at = EXCLUDED.finished_at, "
                f"summary_yaml = EXCLUDED.summary_yaml",
                summary.run_id,
                self._namespace_blob,
                summary.started_at,
                summary.finished_at,
                summary.model_dump_json(),
            )

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    # ---- Read methods (v0.2) ----

    async def list_run_ids(self) -> list[str]:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT run_id FROM {self.schema}.eval_runs ORDER BY run_id"
            )
        return [row["run_id"] for row in rows]

    async def load_summary(self, run_id: str) -> RunSummary | None:
        pool = self._require_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT summary_yaml FROM {self.schema}.eval_runs WHERE run_id = $1",
                run_id,
            )
        if row is None or row["summary_yaml"] is None:
            return None
        return RunSummary.model_validate_json(row["summary_yaml"])

    async def iter_traces(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[Trace]:
        async for payload in self._stream_payloads(
            "eval_traces", run_id, batch_size, order_by="started_at"
        ):
            yield Trace.model_validate_json(payload)

    async def iter_results(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[EvaluationResult]:
        async for payload in self._stream_payloads(
            "eval_results", run_id, batch_size, order_by="started_at, evaluator"
        ):
            yield EvaluationResult.model_validate_json(payload)

    async def _stream_payloads(
        self,
        table: str,
        run_id: str | None,
        batch_size: int,
        *,
        order_by: str,
    ) -> AsyncIterator[str]:
        pool = self._require_pool()
        sql = f"SELECT payload FROM {self.schema}.{table}"
        args: list[Any] = []
        if run_id is not None:
            sql += " WHERE run_id = $1"
            args.append(run_id)
        sql += f" ORDER BY {order_by}"
        # Server-side cursor — bounded memory regardless of result-set size.
        async with pool.acquire() as conn, conn.transaction():
            cur = conn.cursor(sql, *args, prefetch=max(1, batch_size))
            async for record in cur:
                payload = record["payload"]
                yield payload if isinstance(payload, str) else json.dumps(payload)

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise AdapterError("PostgresStore used before open() was called")
        return self._pool


def _is_safe_identifier(name: str) -> bool:
    if not name:
        return False
    return all(ch.isalnum() or ch == "_" for ch in name)
