from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def make_run_id(eval_name: str) -> str:
    iso = utc_now().strftime("%Y-%m-%dT%H:%M:%S")
    return f"{iso}_{eval_name}".replace(":", "-")
