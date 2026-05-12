"""Per-model price tables and cost computation.

The runner uses this to fill `Trace.metrics.cost_usd` when an adapter reported
token counts but no $ figure. Ships `DEFAULT_PRICE_TABLE` with a small set of
current-generation models and a `freshness_date`; the runner emits a single
warning per run when the default is in use so the staleness is visible. Users
override via `eval.yaml > metrics.price_table_path`.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field

from eval_harness.core.errors import ConfigError

logger = logging.getLogger(__name__)

_FORBID = ConfigDict(extra="forbid")


class ModelPrice(BaseModel):
    """Per-million-token pricing for one model.

    Thinking tokens (extended-thinking / reasoning) are billed separately on
    some providers; default to 0.0 when a model has no thinking surcharge.
    """

    model_config = _FORBID
    input_per_million_tokens: float
    output_per_million_tokens: float
    thinking_per_million_tokens: float = 0.0


class PriceTable(BaseModel):
    """Versioned, dated price table.

    `freshness_date` is the day prices were verified against provider
    documentation. The runner warns once per run when `DEFAULT_PRICE_TABLE` is
    in use so users know the cost figures may be stale.
    """

    model_config = _FORBID
    table_version: str
    freshness_date: date
    models: dict[str, ModelPrice] = Field(default_factory=dict)


# Sources verified 2026-05-12 against provider docs:
#   - Anthropic: https://www.anthropic.com/pricing#anthropic-api
#   - OpenAI:    https://openai.com/api/pricing/
# Prices are USD per million tokens. Thinking-token pricing covers extended
# reasoning where the provider bills a distinct rate (e.g. Anthropic 1M
# extended thinking pricing matches output rate at time of capture).
DEFAULT_PRICE_TABLE = PriceTable(
    table_version="2026-05-12",
    freshness_date=date(2026, 5, 12),
    models={
        "claude-opus-4-7": ModelPrice(
            input_per_million_tokens=15.0,
            output_per_million_tokens=75.0,
            thinking_per_million_tokens=75.0,
        ),
        "claude-sonnet-4-6": ModelPrice(
            input_per_million_tokens=3.0,
            output_per_million_tokens=15.0,
            thinking_per_million_tokens=15.0,
        ),
        "claude-haiku-4-5-20251001": ModelPrice(
            input_per_million_tokens=0.25,
            output_per_million_tokens=1.25,
        ),
        "claude-4-7": ModelPrice(
            input_per_million_tokens=3.0,
            output_per_million_tokens=15.0,
            thinking_per_million_tokens=15.0,
        ),
        "gpt-5": ModelPrice(
            input_per_million_tokens=5.0,
            output_per_million_tokens=15.0,
        ),
    },
)


def load_price_table(path: Path | None) -> PriceTable:
    """Load a price table from YAML; `None` returns `DEFAULT_PRICE_TABLE`."""
    if path is None:
        return DEFAULT_PRICE_TABLE
    if not path.exists():
        raise ConfigError(f"price_table_path does not exist: {path}")
    try:
        data = yaml.safe_load(path.read_text())
    except yaml.YAMLError as e:
        raise ConfigError(f"price_table_path is not valid YAML ({path}): {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(
            f"price_table_path must be a YAML mapping ({path}); got {type(data).__name__}"
        )
    return PriceTable.model_validate(data)


def compute_cost(
    table: PriceTable,
    model: str,
    token_input: int,
    token_output: int,
    token_thinking: int = 0,
) -> float | None:
    """Compute the call's $ cost from token counts. Returns `None` when the
    model is not in the table — callers handle the gap (e.g. leave
    `Trace.metrics.cost_usd` as `None`)."""
    price = table.models.get(model)
    if price is None:
        return None
    return (
        (token_input / 1_000_000.0) * price.input_per_million_tokens
        + (token_output / 1_000_000.0) * price.output_per_million_tokens
        + (token_thinking / 1_000_000.0) * price.thinking_per_million_tokens
    )


def warn_default_table_in_use(table: PriceTable) -> None:
    """Emit a single warning when `DEFAULT_PRICE_TABLE` is the active table.

    Idempotent: the runner calls this once at startup. Kept as a function so
    other entry points (e.g. ad-hoc tooling) can opt-in to the same warning.
    """
    if table is DEFAULT_PRICE_TABLE:
        logger.warning(
            "Using DEFAULT_PRICE_TABLE (freshness_date=%s, table_version=%s). "
            "Prices may be stale. Override via eval.yaml > metrics.price_table_path.",
            table.freshness_date.isoformat(),
            table.table_version,
        )


__all__ = [
    "DEFAULT_PRICE_TABLE",
    "ModelPrice",
    "PriceTable",
    "compute_cost",
    "load_price_table",
    "warn_default_table_in_use",
]
