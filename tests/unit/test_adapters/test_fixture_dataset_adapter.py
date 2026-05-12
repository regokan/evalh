from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from eval_harness.adapters.dataset.fixture_dataset_adapter import (
    FixtureDatasetAdapter,
)
from eval_harness.core.errors import ConfigError
from eval_harness.core.models import Trace

_FIXTURE = {
    "schema_version": "1.0",
    "cases": [
        {
            "id": "case_001",
            "input": {"user_message": "What is in ABC123?"},
            "metadata": {"source": "langfuse-production"},
            "embedded_trace": {
                "run_id": "original_run",
                "case_id": "case_001",
                "variant_name": "production",
                "started_at": "2026-05-01T10:00:00Z",
                "finished_at": "2026-05-01T10:00:03Z",
                "latency_ms": 3000,
                "input": {"user_message": "What is in ABC123?"},
                "output": {"final_answer": "Richmond"},
                "metrics": {
                    "token_input": 100,
                    "token_output": 50,
                    "cost_usd": 0.012,
                },
                "extra": {"trace_id": "lf_abc123"},
            },
        },
        {
            "id": "case_002",
            "input": {"user_message": "What is in XYZ?"},
            "embedded_trace": {
                "run_id": "original_run",
                "case_id": "case_002",
                "variant_name": "production",
                "started_at": "2026-05-01T11:00:00Z",
                "finished_at": "2026-05-01T11:00:01Z",
                "latency_ms": 1000,
                "input": {"user_message": "What is in XYZ?"},
                "output": {"final_answer": "Brunswick"},
            },
        },
    ],
}


def _write_fixture(tmp_path: Path) -> Path:
    p = tmp_path / "cases.yaml"
    p.write_text(yaml.safe_dump(_FIXTURE))
    return p


async def test_emits_cases_with_embedded_traces(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    adapter = FixtureDatasetAdapter(path=str(path), embed_full_trace=True)
    assert adapter.embed_full_trace is True

    cases = await adapter.load_cases()
    assert [c.id for c in cases] == ["case_001", "case_002"]

    t1 = cases[0]._embedded_trace
    assert isinstance(t1, Trace)
    assert t1.output.final_answer == "Richmond"
    assert t1.latency_ms == 3000
    assert t1.metrics.token_input == 100
    assert t1.extra["trace_id"] == "lf_abc123"


async def test_embed_full_trace_false_skips_attachment(tmp_path: Path) -> None:
    path = _write_fixture(tmp_path)
    adapter = FixtureDatasetAdapter(path=str(path))  # default False
    assert adapter.embed_full_trace is False
    cases = await adapter.load_cases()
    for c in cases:
        assert c._embedded_trace is None


async def test_missing_embedded_trace_when_required_raises(tmp_path: Path) -> None:
    bad = {
        "schema_version": "1.0",
        "cases": [
            {"id": "case_no_trace", "input": {"q": 1}},  # no embedded_trace
        ],
    }
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad))
    adapter = FixtureDatasetAdapter(path=str(path), embed_full_trace=True)
    with pytest.raises(ConfigError, match="missing 'embedded_trace'"):
        await adapter.load_cases()


async def test_invalid_embedded_trace_payload_raises(tmp_path: Path) -> None:
    bad = {
        "schema_version": "1.0",
        "cases": [
            {
                "id": "c1",
                "input": {"q": 1},
                "embedded_trace": {
                    # Missing required fields like started_at/finished_at.
                    "run_id": "x",
                    "case_id": "c1",
                    "variant_name": "v1",
                    "input": {},
                    "output": {},
                    "latency_ms": 1,
                },
            }
        ],
    }
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad))
    adapter = FixtureDatasetAdapter(path=str(path), embed_full_trace=True)
    with pytest.raises(ConfigError, match="embedded_trace invalid"):
        await adapter.load_cases()


async def test_duplicate_ids_rejected(tmp_path: Path) -> None:
    dup = {
        "schema_version": "1.0",
        "cases": [
            {"id": "c1", "input": {}},
            {"id": "c1", "input": {}},
        ],
    }
    path = tmp_path / "dup.yaml"
    path.write_text(yaml.safe_dump(dup))
    adapter = FixtureDatasetAdapter(path=str(path))
    with pytest.raises(ConfigError, match="duplicate case id 'c1'"):
        await adapter.load_cases()


def test_factory_builds_fixture_adapter(tmp_path: Path) -> None:
    from eval_harness.factories import dataset_adapter_factory

    assert "fixture" in dataset_adapter_factory.registry.names()
    path = _write_fixture(tmp_path)
    inst = dataset_adapter_factory.build(
        {"type": "fixture", "path": str(path), "embed_full_trace": True}
    )
    assert isinstance(inst, FixtureDatasetAdapter)
    assert inst.embed_full_trace is True
