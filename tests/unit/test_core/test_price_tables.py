from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import pytest

from eval_harness.core.errors import ConfigError
from eval_harness.core.price_tables import (
    DEFAULT_PRICE_TABLE,
    ModelPrice,
    PriceTable,
    compute_cost,
    load_price_table,
    warn_default_table_in_use,
)


def test_default_table_loads() -> None:
    table = load_price_table(None)
    assert table is DEFAULT_PRICE_TABLE
    assert isinstance(table.freshness_date, date)
    assert table.table_version
    # Documented in the spec: ~3-5 current-generation models.
    assert 3 <= len(table.models) <= 8
    assert "claude-4-7" in table.models
    for price in table.models.values():
        assert price.input_per_million_tokens >= 0
        assert price.output_per_million_tokens >= 0
        assert price.thinking_per_million_tokens >= 0


def test_compute_cost_correct_for_known_model() -> None:
    table = PriceTable(
        table_version="t",
        freshness_date=date(2026, 5, 1),
        models={
            "demo-1": ModelPrice(
                input_per_million_tokens=3.0,
                output_per_million_tokens=15.0,
            ),
        },
    )
    cost = compute_cost(table, "demo-1", token_input=1_000_000, token_output=500_000)
    # 1M * $3 + 0.5M * $15 = $3 + $7.5 = $10.5
    assert cost == pytest.approx(10.5)


def test_compute_cost_returns_none_for_unknown_model() -> None:
    cost = compute_cost(
        DEFAULT_PRICE_TABLE, "made-up-model-99", token_input=1, token_output=1
    )
    assert cost is None


def test_user_override_replaces_default(tmp_path: Path) -> None:
    override = tmp_path / "prices.yaml"
    override.write_text(
        """
table_version: custom-2026-05-12
freshness_date: 2026-05-12
models:
  demo-1:
    input_per_million_tokens: 1.0
    output_per_million_tokens: 2.0
"""
    )
    table = load_price_table(override)
    assert table is not DEFAULT_PRICE_TABLE
    assert table.table_version == "custom-2026-05-12"
    assert "demo-1" in table.models
    assert "claude-4-7" not in table.models  # the override fully replaces


def test_thinking_tokens_priced_separately() -> None:
    table = PriceTable(
        table_version="t",
        freshness_date=date(2026, 5, 1),
        models={
            "demo-thinker": ModelPrice(
                input_per_million_tokens=1.0,
                output_per_million_tokens=2.0,
                thinking_per_million_tokens=10.0,
            ),
        },
    )
    cost_no_thinking = compute_cost(
        table, "demo-thinker", token_input=1_000_000, token_output=1_000_000
    )
    cost_with_thinking = compute_cost(
        table,
        "demo-thinker",
        token_input=1_000_000,
        token_output=1_000_000,
        token_thinking=500_000,
    )
    assert cost_no_thinking == pytest.approx(3.0)
    # Adds 0.5M * $10 = $5 on top.
    assert cost_with_thinking == pytest.approx(8.0)


def test_load_price_table_missing_path_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.yaml"
    with pytest.raises(ConfigError) as exc:
        load_price_table(missing)
    assert "price_table_path" in str(exc.value)


def test_load_price_table_invalid_yaml_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("not a mapping: [oops\n")
    with pytest.raises(ConfigError):
        load_price_table(bad)


def test_load_price_table_non_mapping_raises(tmp_path: Path) -> None:
    bad = tmp_path / "list.yaml"
    bad.write_text("- one\n- two\n")
    with pytest.raises(ConfigError) as exc:
        load_price_table(bad)
    assert "mapping" in str(exc.value)


def test_load_price_table_validates_schema(tmp_path: Path) -> None:
    from pydantic import ValidationError

    bad = tmp_path / "schema.yaml"
    bad.write_text("table_version: v1\nmodels: {}\n")  # missing freshness_date
    with pytest.raises(ValidationError):
        load_price_table(bad)


def test_warn_default_table_in_use_emits_once_per_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="eval_harness.core.price_tables"):
        warn_default_table_in_use(DEFAULT_PRICE_TABLE)
    matched = [r for r in caplog.records if "DEFAULT_PRICE_TABLE" in r.getMessage()]
    assert len(matched) == 1
    assert DEFAULT_PRICE_TABLE.freshness_date.isoformat() in matched[0].getMessage()


def test_warn_default_table_silent_for_custom_table(
    caplog: pytest.LogCaptureFixture,
) -> None:
    custom = PriceTable(
        table_version="x",
        freshness_date=date(2026, 5, 1),
        models={},
    )
    with caplog.at_level(logging.WARNING, logger="eval_harness.core.price_tables"):
        warn_default_table_in_use(custom)
    assert not any("DEFAULT_PRICE_TABLE" in r.getMessage() for r in caplog.records)


def test_model_price_forbids_unknown_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ModelPrice(
            input_per_million_tokens=1.0,
            output_per_million_tokens=2.0,
            unknown_field=99,  # type: ignore[call-arg]
        )
