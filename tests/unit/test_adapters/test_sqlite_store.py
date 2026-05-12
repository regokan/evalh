from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import pytest

from eval_harness.adapters.trace.sqlite_store import SqliteStore
from eval_harness.core.errors import AdapterError
from eval_harness.core.models import (
    EvaluationResult,
    FileDiff,
    FileManifest,
    FilesystemArtifact,
    RunSummary,
    Trace,
    TraceMetrics,
    TraceOutput,
)

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


def _trace(case_id: str = "c1", variant: str = "v1") -> Trace:
    return Trace(
        run_id="r1",
        case_id=case_id,
        variant_name=variant,
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=10,
        input={"q": case_id},
        output=TraceOutput(final_answer="ok"),
        metrics=TraceMetrics(token_input=5, cost_usd=0.0123),
    )


def _result(case_id: str = "c1", variant: str = "v1", name: str = "ev") -> EvaluationResult:
    return EvaluationResult(
        run_id="r1",
        case_id=case_id,
        variant_name=variant,
        evaluator=name,
        evaluator_type="contains_text",
        passed=True,
        reason="ok",
        started_at=_NOW,
        finished_at=_NOW,
        latency_ms=1,
    )


async def test_open_creates_tables(tmp_path: Path) -> None:
    store = SqliteStore(path=str(tmp_path / "store.db"))
    await store.open("r1", tmp_path)
    await store.close()

    async with (
        aiosqlite.connect(str(tmp_path / "store.db")) as db,
        db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cur,
    ):
        names = [row[0] for row in await cur.fetchall()]
    assert names == ["artifacts", "results", "summaries", "traces"]


async def test_save_trace_roundtrip(tmp_path: Path) -> None:
    db_path = tmp_path / "store.db"
    store = SqliteStore(path=str(db_path))
    await store.open("r1", tmp_path)
    await store.save_trace(_trace("c1", "v1"))
    await store.save_trace(_trace("c2", "v1"))
    await store.close()

    async with (
        aiosqlite.connect(str(db_path)) as db,
        db.execute(
            "SELECT case_id, variant_name, "
            "json_extract(payload, '$.metrics.cost_usd') AS cost "
            "FROM traces ORDER BY case_id"
        ) as cur,
    ):
        rows = await cur.fetchall()
    assert [r[0] for r in rows] == ["c1", "c2"]
    assert all(r[1] == "v1" for r in rows)
    assert pytest.approx(rows[0][2]) == 0.0123


async def test_save_trace_replaces_on_conflict(tmp_path: Path) -> None:
    store = SqliteStore(path=str(tmp_path / "store.db"))
    await store.open("r1", tmp_path)
    await store.save_trace(_trace("c1", "v1"))
    await store.save_trace(_trace("c1", "v1"))
    await store.close()

    async with (
        aiosqlite.connect(str(tmp_path / "store.db")) as db,
        db.execute("SELECT COUNT(*) FROM traces") as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    assert row[0] == 1


async def test_save_evaluation_inserts_multiple(tmp_path: Path) -> None:
    store = SqliteStore(path=str(tmp_path / "store.db"))
    await store.open("r1", tmp_path)
    await store.save_evaluation(
        "c1", "v1", [_result(name="a"), _result(name="b")]
    )
    await store.save_evaluation("c1", "v1", [])  # no-op
    await store.close()

    async with (
        aiosqlite.connect(str(tmp_path / "store.db")) as db,
        db.execute("SELECT evaluator FROM results ORDER BY evaluator") as cur,
    ):
        rows = [r[0] for r in await cur.fetchall()]
    assert rows == ["a", "b"]


async def test_save_summary_serializes_via_pydantic_json(tmp_path: Path) -> None:
    store = SqliteStore(path=str(tmp_path / "store.db"))
    await store.open("r1", tmp_path)
    summary = RunSummary(
        run_id="r1",
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="deadbeef",
        cases_total=3,
        variants=[],
        by_evaluator=[],
    )
    await store.save_summary(summary)
    await store.close()

    async with (
        aiosqlite.connect(str(tmp_path / "store.db")) as db,
        db.execute("SELECT payload FROM summaries WHERE run_id=?", ("r1",)) as cur,
    ):
        row = await cur.fetchone()
    assert row is not None
    parsed = json.loads(row[0])
    assert parsed["run_id"] == "r1"
    assert parsed["cases_total"] == 3
    rebuilt = RunSummary.model_validate(parsed)
    assert rebuilt.run_id == "r1"


async def test_save_artifact_persists(tmp_path: Path) -> None:
    store = SqliteStore(path=str(tmp_path / "store.db"))
    await store.open("r1", tmp_path)
    artifact = FilesystemArtifact(
        case_id="c1",
        variant_name="v1",
        workspace_kind="tempdir_snapshot",
        before_manifest=FileManifest(files={}),
        after_manifest=FileManifest(files={}),
        diff=FileDiff(added=[], removed=[], modified=[]),
        artifacts_path=str(tmp_path / "a"),
    )
    await store.save_artifact(artifact)
    await store.close()

    async with (
        aiosqlite.connect(str(tmp_path / "store.db")) as db,
        db.execute("SELECT case_id, variant_name FROM artifacts") as cur,
    ):
        rows = await cur.fetchall()
    assert rows == [("c1", "v1")]


async def test_save_before_open_raises(tmp_path: Path) -> None:
    store = SqliteStore(path=str(tmp_path / "store.db"))
    with pytest.raises(AdapterError, match=r"before open"):
        await store.save_trace(_trace())


def test_factory_builds_sqlite_store(tmp_path: Path) -> None:
    from eval_harness.factories.trace_store_factory import TraceStoreFactory

    factory = TraceStoreFactory()
    factory.load_entry_points()
    store = factory.build({"type": "sqlite", "path": str(tmp_path / "store.db")})
    assert isinstance(store, SqliteStore)
