"""SQLite trace store. Optional install — `pip install 'eval-harness[sqlite]'`."""

from __future__ import annotations

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
    payload       TEXT NOT NULL,
    PRIMARY KEY (run_id, case_id, variant_name)
);

CREATE TABLE IF NOT EXISTS results (
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    variant_name  TEXT NOT NULL,
    evaluator     TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    payload       TEXT NOT NULL,
    PRIMARY KEY (run_id, case_id, variant_name, evaluator)
);

CREATE TABLE IF NOT EXISTS artifacts (
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    variant_name  TEXT NOT NULL,
    payload       TEXT NOT NULL,
    PRIMARY KEY (run_id, case_id, variant_name)
);

CREATE TABLE IF NOT EXISTS summaries (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    payload       TEXT NOT NULL
);
"""


class SqliteStore:
    """Append-mostly TraceStore backed by aiosqlite.

    One table per data type, payload stored as JSON text (Pydantic
    ``model_dump_json``). Queries use ``json_extract`` for ad-hoc analysis.
    See ``docs/Adapters.md`` for the contract.
    """

    def __init__(self, path: str | Path, **_kwargs: Any) -> None:
        try:
            import aiosqlite as _aiosqlite
        except ImportError as e:
            raise ConfigError(
                "sqlite trace store requested but `aiosqlite` is not "
                "installed. Install with: pip install 'eval-harness[sqlite]'"
            ) from e

        self._aiosqlite = _aiosqlite
        self.path = str(path)
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
        await self._conn.commit()

    async def save_trace(self, trace: Trace) -> None:
        conn = self._require_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO traces "
            "(run_id, case_id, variant_name, started_at, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                trace.run_id,
                trace.case_id,
                trace.variant_name,
                trace.started_at.isoformat(),
                trace.model_dump_json(),
            ),
        )
        await conn.commit()

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
                r.model_dump_json(),
            )
            for r in results
        ]
        await conn.executemany(
            "INSERT OR REPLACE INTO results "
            "(run_id, case_id, variant_name, evaluator, started_at, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )
        await conn.commit()

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        conn = self._require_conn()
        run_id = self._run_id or ""
        await conn.execute(
            "INSERT OR REPLACE INTO artifacts "
            "(run_id, case_id, variant_name, payload) "
            "VALUES (?, ?, ?, ?)",
            (
                run_id,
                artifact.case_id,
                artifact.variant_name,
                artifact.model_dump_json(),
            ),
        )
        await conn.commit()

    async def save_summary(self, summary: RunSummary) -> None:
        conn = self._require_conn()
        await conn.execute(
            "INSERT OR REPLACE INTO summaries (run_id, started_at, payload) "
            "VALUES (?, ?, ?)",
            (summary.run_id, summary.started_at.isoformat(), summary.model_dump_json()),
        )
        await conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    def _require_conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise AdapterError("SqliteStore used before open() was called")
        return self._conn
