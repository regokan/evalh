from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from eval_harness.adapters.trace.local_files_store import (
    LocalFilesStore,
    mask_secrets,
)
from eval_harness.core.errors import AdapterError
from eval_harness.core.models import (
    EvaluationResult,
    FileDiff,
    FileManifest,
    FilesystemArtifact,
    RunSummary,
    Trace,
    TraceOutput,
)


def _make_trace(case_id: str = "case_1", variant: str = "v1") -> Trace:
    now = datetime(2026, 5, 12, tzinfo=UTC)
    return Trace(
        run_id="r1",
        case_id=case_id,
        variant_name=variant,
        started_at=now,
        finished_at=now,
        latency_ms=10,
        input={"q": "hello"},
        output=TraceOutput(final_answer="ok"),
    )


def _make_result(case_id: str = "case_1", variant: str = "v1") -> EvaluationResult:
    now = datetime(2026, 5, 12, tzinfo=UTC)
    return EvaluationResult(
        run_id="r1",
        case_id=case_id,
        variant_name=variant,
        evaluator="contains_richmond",
        evaluator_type="contains_text",
        passed=True,
        reason="match",
        started_at=now,
        finished_at=now,
        latency_ms=2,
    )


def test_mask_secrets_top_level_and_nested() -> None:
    config = {
        "API_KEY": "sk-real",
        "openai": {"api_key": "sk-nested", "model": "gpt-4"},
        "evaluators": [
            {"name": "judge", "ANTHROPIC_API_KEY": "live", "config": {"x": 1}},
            {"name": "other", "password": "hunter2"},
        ],
        "auth": {"BEARER_TOKEN": "abc", "SOME_SECRET": "shh", "user": "alice"},
        "harmless": "kept",
    }
    masked = mask_secrets(config)
    assert masked["API_KEY"] == "***MASKED***"
    assert masked["openai"]["api_key"] == "***MASKED***"
    assert masked["openai"]["model"] == "gpt-4"
    assert masked["evaluators"][0]["ANTHROPIC_API_KEY"] == "***MASKED***"
    assert masked["evaluators"][0]["config"]["x"] == 1
    assert masked["evaluators"][1]["password"] == "***MASKED***"
    assert masked["auth"]["BEARER_TOKEN"] == "***MASKED***"
    assert masked["auth"]["SOME_SECRET"] == "***MASKED***"
    assert masked["auth"]["user"] == "alice"
    assert masked["harmless"] == "kept"

    # Original is untouched.
    assert config["API_KEY"] == "sk-real"


async def test_open_creates_dir_and_masks_config(tmp_path: Path) -> None:
    store = LocalFilesStore(path=str(tmp_path / "runs"))
    store.rendered_config = {
        "run": {"max_concurrency": 4},
        "providers": {"OPENAI_API_KEY": "sk-leak"},
    }
    run_dir = tmp_path / "runs" / "r1"
    await store.open("r1", run_dir)

    assert run_dir.is_dir()
    written = yaml.safe_load((run_dir / "config.yaml").read_text())
    assert written["providers"]["OPENAI_API_KEY"] == "***MASKED***"
    assert written["run"]["max_concurrency"] == 4

    expected_hash = hashlib.sha256(
        yaml.safe_dump(store.rendered_config, sort_keys=False).encode("utf-8")
    ).hexdigest()
    assert (run_dir / "config_hash.txt").read_text().strip() == expected_hash


async def test_open_without_config_skips_config_files(tmp_path: Path) -> None:
    store = LocalFilesStore(path=str(tmp_path / "runs"))
    run_dir = tmp_path / "runs" / "r1"
    await store.open("r1", run_dir)
    assert run_dir.is_dir()
    assert not (run_dir / "config.yaml").exists()
    assert not (run_dir / "config_hash.txt").exists()


async def test_save_trace_appends_jsonl(tmp_path: Path) -> None:
    store = LocalFilesStore(path=str(tmp_path))
    run_dir = tmp_path / "r1"
    await store.open("r1", run_dir)

    await store.save_trace(_make_trace("a"))
    await store.save_trace(_make_trace("b"))

    lines = (run_dir / "traces.jsonl").read_text().splitlines()
    assert len(lines) == 2
    parsed = [json.loads(line) for line in lines]
    assert [p["case_id"] for p in parsed] == ["a", "b"]


async def test_save_evaluation_appends_results(tmp_path: Path) -> None:
    store = LocalFilesStore(path=str(tmp_path))
    run_dir = tmp_path / "r1"
    await store.open("r1", run_dir)

    await store.save_evaluation("a", "v1", [_make_result("a"), _make_result("a")])
    await store.save_evaluation("a", "v1", [])  # empty no-op

    lines = (run_dir / "results.jsonl").read_text().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)  # each line valid JSON


async def test_save_summary_round_trips(tmp_path: Path) -> None:
    store = LocalFilesStore(path=str(tmp_path))
    run_dir = tmp_path / "r1"
    await store.open("r1", run_dir)

    now = datetime(2026, 5, 12, tzinfo=UTC)
    summary = RunSummary(
        run_id="r1",
        started_at=now,
        finished_at=now,
        config_path="eval.yaml",
        config_hash="deadbeef",
        cases_total=3,
        variants=[],
        by_evaluator=[],
    )
    await store.save_summary(summary)

    loaded = yaml.safe_load((run_dir / "summary.yaml").read_text())
    assert loaded["run_id"] == "r1"
    assert loaded["cases_total"] == 3
    round_trip = RunSummary.model_validate(loaded)
    assert round_trip.run_id == "r1"


async def test_save_artifact_writes_tree(tmp_path: Path) -> None:
    store = LocalFilesStore(path=str(tmp_path))
    run_dir = tmp_path / "r1"
    await store.open("r1", run_dir)

    artifact = FilesystemArtifact(
        case_id="case_x",
        variant_name="variant_y",
        workspace_kind="tempdir_snapshot",
        before_manifest=FileManifest(files={}),
        after_manifest=FileManifest(files={}),
        diff=FileDiff(added=[], removed=[], modified=[]),
        artifacts_path=str(run_dir / "artifacts" / "case_x" / "variant_y"),
    )
    await store.save_artifact(artifact)

    out = run_dir / "artifacts" / "case_x" / "variant_y" / "artifact.json"
    assert out.is_file()
    parsed = json.loads(out.read_text())
    assert parsed["case_id"] == "case_x"
    assert parsed["variant_name"] == "variant_y"


async def test_save_trace_before_open_raises(tmp_path: Path) -> None:
    store = LocalFilesStore(path=str(tmp_path))
    with pytest.raises(AdapterError, match="before open"):
        await store.save_trace(_make_trace())
