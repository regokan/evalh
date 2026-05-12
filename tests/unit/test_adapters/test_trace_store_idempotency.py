"""Parametrized v2 idempotency tests for the canonical trace stores.

The three canonical sinks (local_files, sqlite, postgres) must satisfy
the `save_trace_idempotent(trace, cell_id)` contract:

  - First save with a fresh `cell_id` returns True; the trace is on disk.
  - Re-submitting the same `cell_id` after a SUCCESSFUL save is a no-op
    (returns False); the original record is preserved.
  - Re-submitting after an ERROR-state save is a retry: the new record
    overwrites the previous one (returns True).

postgres runs only when `EVALH_TEST_POSTGRES_DSN` is set in the env;
without it the parametrization skips that backend.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from eval_harness.adapters.trace.local_files_store import LocalFilesStore
from eval_harness.core.models import (
    Trace,
    TraceError,
    TraceMetrics,
    TraceOutput,
)

if TYPE_CHECKING:
    from eval_harness.adapters.trace.base import TraceStore

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


def _build_local_files(tmp_path: Path) -> TraceStore:
    store = LocalFilesStore(path=str(tmp_path / "runs"))
    store.rendered_config = {"eval": {"name": "demo"}}
    return store  # type: ignore[return-value]


def _build_sqlite(tmp_path: Path) -> TraceStore:
    from eval_harness.adapters.trace.sqlite_store import SqliteStore

    return SqliteStore(path=str(tmp_path / "store.db"))  # type: ignore[return-value]


def _build_postgres(tmp_path: Path) -> TraceStore:
    from eval_harness.adapters.trace.postgres_store import PostgresStore

    dsn = os.environ["EVALH_TEST_POSTGRES_DSN"]
    schema = f"test_idempotent_{abs(hash(str(tmp_path))) % 10_000_000:07d}"
    return PostgresStore(dsn=dsn, schema=schema)  # type: ignore[return-value]


_BUILDERS: list[tuple[str, Callable[[Path], TraceStore]]] = [
    ("local_files", _build_local_files),
    ("sqlite", _build_sqlite),
]

if os.environ.get("EVALH_TEST_POSTGRES_DSN"):
    try:
        import asyncpg  # noqa: F401

        _BUILDERS.append(("postgres", _build_postgres))
    except ImportError:
        pass


@pytest.fixture(params=_BUILDERS, ids=lambda b: b[0])
def store_builder(request: pytest.FixtureRequest) -> Callable[[Path], TraceStore]:
    return request.param[1]  # type: ignore[no-any-return]


def _trace(
    *,
    run_id: str = "r1",
    case_id: str = "c1",
    variant: str = "v1",
    final_answer: str = "first",
    error: TraceError | None = None,
) -> Trace:
    return Trace(
        run_id=run_id,
        case_id=case_id,
        variant_name=variant,
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=10,
        input={"q": case_id},
        output=TraceOutput(final_answer=final_answer),
        metrics=TraceMetrics(token_input=5),
        error=error,
    )


async def test_first_save_returns_true(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    store = store_builder(tmp_path)
    await store.open("r1", tmp_path / "runs" / "r1")
    try:
        wrote = await store.save_trace_idempotent(
            _trace(), cell_id="r1::c1::v1::aaaaaaaaaaaa"
        )
        assert wrote is True
    finally:
        await store.close()


async def test_replay_after_success_returns_false_and_preserves(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    """Re-submitting a SUCCESSFUL cell_id is a no-op — the first record
    survives even if the caller hands us a `trace` with different
    content."""
    store = store_builder(tmp_path)
    await store.open("r1", tmp_path / "runs" / "r1")
    try:
        cell_id = "r1::c1::v1::bbbbbbbbbbbb"
        first = await store.save_trace_idempotent(
            _trace(final_answer="first"), cell_id=cell_id
        )
        assert first is True

        # Re-submit with a different payload — must be a no-op.
        second = await store.save_trace_idempotent(
            _trace(final_answer="second"), cell_id=cell_id
        )
        assert second is False
    finally:
        await store.close()


async def test_replay_after_error_overwrites_with_retry(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    """A failed cell (`trace.error != None`) is a retry candidate. A
    second `save_trace_idempotent` with the same cell_id overwrites the
    error record with the new attempt — returns True (wrote)."""
    store = store_builder(tmp_path)
    await store.open("r1", tmp_path / "runs" / "r1")
    try:
        cell_id = "r1::c1::v1::cccccccccccc"
        first = await store.save_trace_idempotent(
            _trace(error=TraceError(type="timeout", message="boom")),
            cell_id=cell_id,
        )
        assert first is True

        retry = await store.save_trace_idempotent(
            _trace(final_answer="after-retry"), cell_id=cell_id
        )
        assert retry is True
    finally:
        await store.close()


async def test_different_cell_ids_dont_collide(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    store = store_builder(tmp_path)
    await store.open("r1", tmp_path / "runs" / "r1")
    try:
        a = await store.save_trace_idempotent(
            _trace(case_id="c1"), cell_id="r1::c1::v1::dddddddddddd"
        )
        b = await store.save_trace_idempotent(
            _trace(case_id="c2"), cell_id="r1::c2::v1::eeeeeeeeeeee"
        )
        assert a is True
        assert b is True
    finally:
        await store.close()


async def test_third_attempt_after_two_successes_still_no_op(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    """Sanity: an excess attempt past the second is still a no-op. The
    contract doesn't store a counter — it just guards by error_type."""
    store = store_builder(tmp_path)
    await store.open("r1", tmp_path / "runs" / "r1")
    try:
        cell_id = "r1::c1::v1::ffffffffffff"
        assert await store.save_trace_idempotent(_trace(), cell_id=cell_id) is True
        assert await store.save_trace_idempotent(_trace(), cell_id=cell_id) is False
        assert await store.save_trace_idempotent(_trace(), cell_id=cell_id) is False
    finally:
        await store.close()
