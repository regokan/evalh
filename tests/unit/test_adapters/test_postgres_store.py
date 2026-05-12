"""Backend-specific tests for the Postgres trace store.

Most behaviour is covered by ``test_trace_stores_common.py``; this module
adds the postgres-only checks (DSN validation, schema injection guard,
ConfigError when asyncpg is missing). The roundtrip test against a real
instance lives in ``test_trace_stores_common.py`` and is gated on
``EVALH_TEST_POSTGRES_DSN``.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from eval_harness.core.errors import ConfigError

if TYPE_CHECKING:
    from eval_harness.adapters.trace.postgres_store import PostgresStore


def _have_asyncpg() -> bool:
    return importlib.util.find_spec("asyncpg") is not None


def _import_store() -> type[PostgresStore]:
    from eval_harness.adapters.trace.postgres_store import PostgresStore

    return PostgresStore


@pytest.mark.skipif(not _have_asyncpg(), reason="asyncpg not installed")
def test_postgres_store_requires_dsn() -> None:
    Store = _import_store()
    with pytest.raises(ConfigError, match="dsn"):
        Store()


@pytest.mark.skipif(not _have_asyncpg(), reason="asyncpg not installed")
def test_postgres_store_rejects_unsafe_schema() -> None:
    Store = _import_store()
    with pytest.raises(ConfigError, match="schema"):
        Store(dsn="postgres://x", schema="public; DROP TABLE eval_runs --")


@pytest.mark.skipif(not _have_asyncpg(), reason="asyncpg not installed")
def test_postgres_store_accepts_valid_identifier_schema() -> None:
    Store = _import_store()
    store = Store(dsn="postgres://x", schema="my_namespace_42")
    assert store.schema == "my_namespace_42"


def test_postgres_store_missing_asyncpg_raises_clean_config_error() -> None:
    """When asyncpg isn't installed, instantiation must raise ConfigError with
    the expected install hint — not ImportError."""
    if _have_asyncpg():
        # Simulate asyncpg being missing by hiding it from sys.modules.
        # The store catches the ImportError inside __init__.
        original = sys.modules.pop("asyncpg", None)
        try:
            sys.modules["asyncpg"] = None  # type: ignore[assignment]
            # Force re-import of postgres_store to trigger the import attempt
            # inside __init__ rather than at module load.
            from eval_harness.adapters.trace.postgres_store import PostgresStore

            with pytest.raises(ConfigError, match="eval-harness\\[postgres\\]"):
                PostgresStore(dsn="postgres://x")
        finally:
            sys.modules.pop("asyncpg", None)
            if original is not None:
                sys.modules["asyncpg"] = original
    else:
        from eval_harness.adapters.trace.postgres_store import PostgresStore

        with pytest.raises(ConfigError, match="eval-harness\\[postgres\\]"):
            PostgresStore(dsn="postgres://x")


@pytest.mark.skipif(
    not os.environ.get("EVALH_TEST_POSTGRES_DSN"),
    reason="set EVALH_TEST_POSTGRES_DSN to run live-postgres tests",
)
@pytest.mark.skipif(not _have_asyncpg(), reason="asyncpg not installed")
async def test_postgres_store_roundtrip_against_real_instance(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from eval_harness.adapters.trace.postgres_store import PostgresStore
    from eval_harness.core.models import RunSummary, Trace, TraceOutput

    schema = f"test_evalh_{abs(hash(str(tmp_path))) % 10_000_000:07d}"
    store = PostgresStore(
        dsn=os.environ["EVALH_TEST_POSTGRES_DSN"], schema=schema
    )
    now = datetime(2026, 5, 12, tzinfo=UTC)
    await store.open("r1", tmp_path)
    try:
        await store.save_trace(
            Trace(
                run_id="r1",
                case_id="c1",
                variant_name="v1",
                started_at=now,
                finished_at=now,
                latency_ms=10,
                input={"q": "hi"},
                output=TraceOutput(final_answer="ok"),
            )
        )
        await store.save_summary(
            RunSummary(
                run_id="r1",
                started_at=now,
                finished_at=now,
                config_path="eval.yaml",
                config_hash="x",
                cases_total=1,
                variants=[],
                by_evaluator=[],
            )
        )
        traces = [t async for t in store.iter_traces(run_id="r1")]
        assert len(traces) == 1
        assert traces[0].case_id == "c1"
        summary = await store.load_summary("r1")
        assert summary is not None
        assert summary.run_id == "r1"
    finally:
        await store.close()
