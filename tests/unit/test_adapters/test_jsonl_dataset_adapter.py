from __future__ import annotations

from pathlib import Path

import pytest

from eval_harness.adapters.dataset.jsonl_dataset_adapter import JsonlDatasetAdapter
from eval_harness.core.errors import AdapterError, ConfigError


async def test_loads_jsonl_cases(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"id": "c1", "input": {"user_message": "hello"}}\n'
        '{"id": "c2", "input": {"user_message": "world"}, '
        '"metadata": {"tag": "x"}}\n'
    )
    adapter = JsonlDatasetAdapter(path=str(path))
    cases = await adapter.load_cases()
    assert [c.id for c in cases] == ["c1", "c2"]
    assert cases[0].input == {"user_message": "hello"}
    assert cases[1].metadata == {"tag": "x"}


async def test_blank_lines_are_skipped(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '\n'
        '{"id": "c1", "input": {"q": 1}}\n'
        '\n'
        '   \n'
        '{"id": "c2", "input": {"q": 2}}\n'
    )
    cases = await JsonlDatasetAdapter(path=str(path)).load_cases()
    assert [c.id for c in cases] == ["c1", "c2"]


async def test_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"id": "dup", "input": {}}\n'
        '{"id": "dup", "input": {}}\n'
    )
    with pytest.raises(ConfigError) as exc:
        await JsonlDatasetAdapter(path=str(path)).load_cases()
    msg = str(exc.value)
    assert "duplicate" in msg
    assert "dup" in msg
    assert ":2:" in msg


async def test_rejects_malformed_json_line_reports_line_number(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"id": "c1", "input": {}}\n'
        '{"id": "c2", "input": {oops not json}\n'
        '{"id": "c3", "input": {}}\n'
    )
    with pytest.raises(ConfigError) as exc:
        await JsonlDatasetAdapter(path=str(path)).load_cases()
    assert ":2:" in str(exc.value)
    assert "malformed JSON" in str(exc.value)


async def test_rejects_non_object_line(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    path.write_text('["not", "an", "object"]\n')
    with pytest.raises(ConfigError) as exc:
        await JsonlDatasetAdapter(path=str(path)).load_cases()
    assert ":1:" in str(exc.value)
    assert "JSON object" in str(exc.value)


async def test_invalid_evalcase_payload_reports_line_number(tmp_path: Path) -> None:
    path = tmp_path / "cases.jsonl"
    # Missing required 'id'.
    path.write_text('{"input": {"x": 1}}\n')
    with pytest.raises(ConfigError) as exc:
        await JsonlDatasetAdapter(path=str(path)).load_cases()
    assert ":1:" in str(exc.value)
    assert "invalid EvalCase" in str(exc.value)


async def test_missing_file_raises_adapter_error(tmp_path: Path) -> None:
    adapter = JsonlDatasetAdapter(path=str(tmp_path / "missing.jsonl"))
    with pytest.raises(AdapterError):
        await adapter.load_cases()


def test_factory_builds_jsonl_adapter(tmp_path: Path) -> None:
    from eval_harness.factories import dataset_adapter_factory

    assert "jsonl" in dataset_adapter_factory.registry.names()

    path = tmp_path / "cases.jsonl"
    path.write_text('{"id": "c1", "input": {}}\n')
    inst = dataset_adapter_factory.build({"type": "jsonl", "path": str(path)})
    assert isinstance(inst, JsonlDatasetAdapter)
    assert inst.path == path
