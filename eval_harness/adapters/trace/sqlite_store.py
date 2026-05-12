"""SQLite trace store. Optional install — `pip install 'eval-harness[sqlite]'`."""

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
    import aiosqlite


_SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    variant_name  TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    namespace     TEXT,
    cell_id       TEXT,
    error_type    TEXT,
    payload       TEXT NOT NULL,
    PRIMARY KEY (run_id, case_id, variant_name)
);

CREATE TABLE IF NOT EXISTS results (
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    variant_name  TEXT NOT NULL,
    evaluator     TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    namespace     TEXT,
    payload       TEXT NOT NULL,
    PRIMARY KEY (run_id, case_id, variant_name, evaluator)
);

CREATE TABLE IF NOT EXISTS artifacts (
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    variant_name  TEXT NOT NULL,
    namespace     TEXT,
    payload       TEXT NOT NULL,
    PRIMARY KEY (run_id, case_id, variant_name)
);

CREATE TABLE IF NOT EXISTS summaries (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    namespace     TEXT,
    payload       TEXT NOT NULL
);
"""


class SqliteStore:
    """Append-mostly TraceStore backed by aiosqlite.

    One table per data type, payload stored as JSON text (Pydantic
    ``model_dump_json``). Queries use ``json_extract`` for ad-hoc analysis.

    ``run_namespace`` is serialized as a JSON ``namespace`` column on every
    row. Multi-tenant isolation is opt-in at query time; v0.2 doesn't add
    an index, but the column is there for downstream tooling and for the
    postgres store to use the same shape. See ``docs/Adapters.md`` and
    ``docs/DataModel.md > Run namespacing``.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        run_namespace: dict[str, str] | None = None,
        **_kwargs: Any,
    ) -> None:
        try:
            import aiosqlite as _aiosqlite
        except ImportError as e:
            raise ConfigError(
                "sqlite trace store requested but `aiosqlite` is not "
                "installed. Install with: pip install 'eval-harness[sqlite]'"
            ) from e

        self._aiosqlite = _aiosqlite
        self.path = str(path)
        self.run_namespace = dict(run_namespace) if run_namespace else None
        self._namespace_blob = (
            json.dumps(self.run_namespace, sort_keys=True)
            if self.run_namespace is not None
            else None
        )
        self._run_id: str | None = None
        self._conn: aiosqlite.Connection | None = None

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
        self._conn = await self._aiosqlite.connect(self.path)
        await self._conn.executescript(_SCHEMA)
        await self._ensure_namespace_columns()
        await self._conn.commit()

    async def _ensure_namespace_columns(self) -> None:
        """Add new columns to existing dbs created by earlier versions.

        - ``namespace`` (v0.2) on every table.
        - ``cell_id`` + ``error_type`` (v2) on the traces table — drives
          the idempotency contract via `save_trace_idempotent`.

        ``ALTER TABLE ADD COLUMN`` is the cheapest path SQLite supports;
        we check ``PRAGMA table_info`` first so reopening an up-to-date
        db is a no-op.
        """
        assert self._conn is not None
        for table in ("traces", "results", "artifacts", "summaries"):
            cur = await self._conn.execute(f"PRAGMA table_info({table})")
            cols = {row[1] async for row in cur}
            await cur.close()
            if "namespace" not in cols:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN namespace TEXT"
                )
        # Traces-only columns for v2 idempotency.
        cur = await self._conn.execute("PRAGMA table_info(traces)")
        traces_cols = {row[1] async for row in cur}
        await cur.close()
        for col in ("cell_id", "error_type"):
            if col not in traces_cols:
                await self._conn.execute(
                    f"ALTER TABLE traces ADD COLUMN {col} TEXT"
                )

    async def save_trace(self, trace: Trace) -> None:
        conn = self._require_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO traces "
            "(run_id, case_id, variant_name, started_at, namespace, "
            " cell_id, error_type, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trace.run_id,
                trace.case_id,
                trace.variant_name,
                trace.started_at.isoformat(),
                self._namespace_blob,
                None,
                trace.error.type if trace.error else None,
                trace.model_dump_json(),
            ),
        )
        await conn.commit()

    async def save_trace_idempotent(self, trace: Trace, cell_id: str) -> bool:
        """v2 idempotency keyed by `cell_id`. No-op when a successful
        record (error_type IS NULL) already exists; overwrite when the
        existing record was an error (retry-after-crash path)."""
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT error_type FROM traces WHERE cell_id = ? LIMIT 1",
            (cell_id,),
        )
        row = await cur.fetchone()
        await cur.close()
        if row is not None and row[0] is None:
            return False
        await conn.execute(
            "INSERT OR REPLACE INTO traces "
            "(run_id, case_id, variant_name, started_at, namespace, "
            " cell_id, error_type, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                trace.run_id,
                trace.case_id,
                trace.variant_name,
                trace.started_at.isoformat(),
                self._namespace_blob,
                cell_id,
                trace.error.type if trace.error else None,
                trace.model_dump_json(),
            ),
        )
        await conn.commit()
        return True

    async def save_evaluation(
        self,
        case_id: str,
        variant: str,
        results: list[EvaluationResult],
    ) -> None:
        if not results:
            return
        conn = self._require_conn()
        rows = [
            (
                r.run_id,
                r.case_id,
                r.variant_name,
                r.evaluator,
                r.started_at.isoformat(),
                self._namespace_blob,
                r.model_dump_json(),
            )
            for r in results
        ]
        await conn.executemany(
            "INSERT OR REPLACE INTO results "
            "(run_id, case_id, variant_name, evaluator, started_at, namespace, payload) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        await conn.commit()

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        conn = self._require_conn()
        run_id = self._run_id or ""
        await conn.execute(
            "INSERT OR REPLACE INTO artifacts "
            "(run_id, case_id, variant_name, namespace, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                run_id,
                artifact.case_id,
                artifact.variant_name,
                self._namespace_blob,
                artifact.model_dump_json(),
            ),
        )
        await conn.commit()

    async def save_summary(self, summary: RunSummary) -> None:
        conn = self._require_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO summaries "
            "(run_id, started_at, namespace, payload) "
            "VALUES (?, ?, ?, ?)",
            (
                summary.run_id,
                summary.started_at.isoformat(),
                self._namespace_blob,
                summary.model_dump_json(),
            ),
        )
        await conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # ---- Read methods (v0.2) ----

    async def list_run_ids(self) -> list[str]:
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT DISTINCT run_id FROM traces ORDER BY run_id"
        )
        ids = [row[0] async for row in cur]
        await cur.close()
        return ids

    async def load_summary(self, run_id: str) -> RunSummary | None:
        conn = self._require_conn()
        cur = await conn.execute(
            "SELECT payload FROM summaries WHERE run_id = ?", (run_id,)
        )
        row = await cur.fetchone()
        await cur.close()
        if row is None:
            return None
        return RunSummary.model_validate_json(row[0])

    async def iter_traces(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[Trace]:
        async for payload in self._stream_payloads(
            "traces", run_id, batch_size, order_by="started_at"
        ):
            yield Trace.model_validate_json(payload)

    async def iter_results(
        self, run_id: str | None = None, batch_size: int = 100
    ) -> AsyncIterator[EvaluationResult]:
        async for payload in self._stream_payloads(
            "results", run_id, batch_size, order_by="started_at, evaluator"
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
        conn = self._require_conn()
        sql = f"SELECT payload FROM {table}"
        params: tuple[str, ...] = ()
        if run_id is not None:
            sql += " WHERE run_id = ?"
            params = (run_id,)
        sql += f" ORDER BY {order_by}"
        cur = await conn.execute(sql, params)
        try:
            while True:
                rows = await cur.fetchmany(max(1, batch_size))
                if not rows:
                    return
                for row in rows:
                    yield row[0]
        finally:
            await cur.close()

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise AdapterError("SqliteStore used before open() was called")
        return self._conn
