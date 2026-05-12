"""Parametrized contract tests covering every registered TraceStore impl.

When a new backend ships (postgres next), it plugs in via the
``eval_harness.trace_stores`` entry-point group and gets full coverage
automatically — no per-backend duplication. Each backend declares how to
build a store instance via the ``_STORE_BUILDERS`` table at the top.
"""
from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from eval_harness.adapters.trace.local_files_store import LocalFilesStore
from eval_harness.core.models import (
    EvaluationResult,
    RunSummary,
    Trace,
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
    # Each test gets its own schema so concurrent runs don't collide on the
    # eval_runs primary key. asyncpg is required; without it the import is
    # caught by the param-skip filter below.
    from eval_harness.adapters.trace.postgres_store import PostgresStore

    dsn = os.environ["EVALH_TEST_POSTGRES_DSN"]
    schema = f"test_evalh_{abs(hash(str(tmp_path))) % 10_000_000:07d}"
    return PostgresStore(dsn=dsn, schema=schema)  # type: ignore[return-value]


_STORE_BUILDERS: list[tuple[str, Callable[[Path], TraceStore]]] = [
    ("local_files", _build_local_files),
    ("sqlite", _build_sqlite),
]

# Plug postgres in only when an ephemeral instance is reachable. Set
# `EVALH_TEST_POSTGRES_DSN=postgres://...` in the test env to enable. CI / dev
# without postgres skip the param entirely — no failures, no flakes.
if os.environ.get("EVALH_TEST_POSTGRES_DSN"):
    try:
        import asyncpg  # noqa: F401

        _STORE_BUILDERS.append(("postgres", _build_postgres))
    except ImportError:
        pass


@pytest.fixture(params=_STORE_BUILDERS, ids=lambda b: b[0])
def store_builder(request: pytest.FixtureRequest) -> Callable[[Path], TraceStore]:
    return request.param[1]  # type: ignore[no-any-return]


def _trace(run_id: str, case_id: str, variant: str) -> Trace:
    return Trace(
        run_id=run_id,
        case_id=case_id,
        variant_name=variant,
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=10,
        input={"q": case_id},
        output=TraceOutput(final_answer=f"ans-{case_id}-{variant}"),
        metrics=TraceMetrics(token_input=5),
    )


def _result(run_id: str, case_id: str, variant: str, evaluator: str) -> EvaluationResult:
    return EvaluationResult(
        run_id=run_id,
        case_id=case_id,
        variant_name=variant,
        evaluator=evaluator,
        evaluator_type="contains_text",
        passed=True,
        reason="ok",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
    )


def _summary(run_id: str, total: int = 2) -> RunSummary:
    return RunSummary(
        run_id=run_id,
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="x",
        cases_total=total,
        variants=[],
        by_evaluator=[],
    )


async def _drain(stream: AsyncIterator[object]) -> list[object]:
    return [item async for item in stream]


async def test_open_save_close_roundtrip(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    store = store_builder(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    await store.open("r1", run_dir)
    try:
        await store.save_trace(_trace("r1", "c1", "v1"))
        await store.save_evaluation(
            "c1", "v1", [_result("r1", "c1", "v1", "ev")]
        )
        await store.save_summary(_summary("r1"))
    finally:
        await store.close()


async def test_iter_traces_yields_all_saved(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    store = store_builder(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    await store.open("r1", run_dir)
    try:
        traces_in = [
            _trace("r1", "c1", "v1"),
            _trace("r1", "c2", "v1"),
            _trace("r1", "c1", "v2"),
        ]
        for t in traces_in:
            await store.save_trace(t)
        # Summary required by some backends' list_run_ids semantics; harmless
        # for the others.
        await store.save_summary(_summary("r1", total=3))

        collected = await _drain(store.iter_traces(run_id="r1"))
    finally:
        await store.close()

    assert len(collected) == 3
    keys = {(t.case_id, t.variant_name) for t in collected if isinstance(t, Trace)}
    assert keys == {("c1", "v1"), ("c2", "v1"), ("c1", "v2")}


async def test_iter_results_yields_all_saved(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    store = store_builder(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    await store.open("r1", run_dir)
    try:
        # local_files iter_results scans the run dir, which requires
        # traces.jsonl to exist first.
        await store.save_trace(_trace("r1", "c1", "v1"))
        await store.save_evaluation(
            "c1",
            "v1",
            [
                _result("r1", "c1", "v1", "a"),
                _result("r1", "c1", "v1", "b"),
            ],
        )
        await store.save_summary(_summary("r1"))

        collected = await _drain(store.iter_results(run_id="r1"))
    finally:
        await store.close()

    evaluators = {r.evaluator for r in collected if isinstance(r, EvaluationResult)}
    assert evaluators == {"a", "b"}


async def test_load_summary_returns_saved(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    store = store_builder(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    await store.open("r1", run_dir)
    try:
        await store.save_trace(_trace("r1", "c1", "v1"))
        await store.save_summary(_summary("r1", total=7))
        loaded = await store.load_summary("r1")
    finally:
        await store.close()

    assert loaded is not None
    assert loaded.run_id == "r1"
    assert loaded.cases_total == 7


async def test_load_summary_returns_none_when_missing(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    store = store_builder(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    await store.open("r1", run_dir)
    try:
        loaded = await store.load_summary("not_a_run")
    finally:
        await store.close()
    assert loaded is None


async def test_list_run_ids_returns_all_runs(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    # Seed two runs via separate store instances (same backend file/dir).
    store = store_builder(tmp_path)
    for run_id in ("ra", "rb"):
        run_dir = tmp_path / "runs" / run_id
        await store.open(run_id, run_dir)
        await store.save_trace(_trace(run_id, "c1", "v1"))
        await store.save_summary(_summary(run_id))
    try:
        ids = await store.list_run_ids()
    finally:
        await store.close()
    assert set(ids) == {"ra", "rb"}


async def test_close_is_idempotent(
    store_builder: Callable[[Path], TraceStore], tmp_path: Path
) -> None:
    store = store_builder(tmp_path)
    run_dir = tmp_path / "runs" / "r1"
    await store.open("r1", run_dir)
    await store.close()
    await store.close()  # second close must not raise
