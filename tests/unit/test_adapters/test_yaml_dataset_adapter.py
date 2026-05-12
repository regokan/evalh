from __future__ import annotations

from pathlib import Path

import pytest

from eval_harness.adapters.dataset.yaml_dataset_adapter import YamlDatasetAdapter
from eval_harness.core.errors import AdapterError, ConfigError

TINY_DEMO_CASES = (
    Path(__file__).resolve().parents[2].parent / "examples" / "tiny_demo" / "cases.yaml"
)


async def test_loads_tiny_demo_cases() -> None:
    adapter = YamlDatasetAdapter(path=TINY_DEMO_CASES)
    cases = await adapter.load_cases()

    assert [c.id for c in cases] == ["tiny_demo_001", "tiny_demo_002", "tiny_demo_003"]

    first = cases[0]
    assert first.input == {
        "user_message": "What is the average house price near listing ABC123?"
    }
    assert first.metadata == {"listing_id": "ABC123", "suburb": "Richmond"}
    assert first.expected.must_call_tools == [
        "get_listing_details",
        "get_average_suburb_price",
    ]
    assert first.expected.answer_should_include == ["Richmond"]
    assert first.expected.facts == {"suburb_average_price": 1200000}

    # Defaults populated for fields not declared in the YAML.
    assert first.expected.answer_should_not_include == []
    assert first.expected.must_modify_files == []
    assert first.expected.must_not_modify_files == []


async def test_rejects_duplicate_case_ids(tmp_path: Path) -> None:
    yaml_path = tmp_path / "cases.yaml"
    yaml_path.write_text(
        """
schema_version: "1.0"
dataset:
  name: dup
cases:
  - id: a
    input: {q: 1}
  - id: a
    input: {q: 2}
"""
    )
    adapter = YamlDatasetAdapter(path=yaml_path)

    with pytest.raises(ConfigError, match="duplicate case id 'a'"):
        await adapter.load_cases()


async def test_rejects_unsupported_schema_version(tmp_path: Path) -> None:
    yaml_path = tmp_path / "cases.yaml"
    yaml_path.write_text(
        """
schema_version: "2.0"
dataset:
  name: future
cases:
  - id: a
    input: {q: 1}
"""
    )
    adapter = YamlDatasetAdapter(path=yaml_path)

    with pytest.raises(ConfigError, match=r"unsupported schema_version '2\.0'"):
        await adapter.load_cases()


async def test_missing_file_raises_adapter_error(tmp_path: Path) -> None:
    adapter = YamlDatasetAdapter(path=tmp_path / "nope.yaml")
    with pytest.raises(AdapterError, match="Cannot read dataset file"):
        await adapter.load_cases()


async def test_default_expected_when_omitted(tmp_path: Path) -> None:
    yaml_path = tmp_path / "cases.yaml"
    yaml_path.write_text(
        """
schema_version: "1.0"
dataset:
  name: bare
cases:
  - id: only
    input: {q: 1}
"""
    )
    adapter = YamlDatasetAdapter(path=yaml_path)
    cases = await adapter.load_cases()
    assert len(cases) == 1
    assert cases[0].expected.must_call_tools == []
    assert cases[0].expected.facts == {}
