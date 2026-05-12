"""Streaming-aggregation perf guard.

Synthesises a 10K-case JSONL dataset, runs the harness end-to-end with a
trivial stub adapter, and asserts the runner stays inside the v0.2 memory
and wall-time budget. This is a *guard*, not a benchmark — the assertions
are deliberately loose so they catch regressions (someone reintroduces
`list(outcomes)`) without flapping on CI hardware variation.

Default pytest invocations exclude this with `-m "not perf"`. Run on demand:

    pytest -m perf tests/perf/test_10k_case_run.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import tracemalloc
import types
from pathlib import Path
from typing import Any

import pytest

_STUB_MODULE = "_evalh_perf_stub_agent"
_NUM_CASES = 10_000
_PEAK_RSS_BUDGET_BYTES = 1 * 1024 * 1024 * 1024  # 1 GB
_WALL_TIME_BUDGET_SECONDS = 30.0


def _install_stub() -> None:
    def agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
        # Cheapest possible "real" return; small but realistic shape.
        return {
            "final_answer": "ok",
            "metrics": {"token_input": 1, "token_output": 1, "cost_usd": 0.0001},
        }

    mod = types.ModuleType(_STUB_MODULE)
    mod.run = agent  # type: ignore[attr-defined]
    sys.modules[_STUB_MODULE] = mod


def _write_dataset(path: Path, n: int) -> None:
    with path.open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"id": f"c{i:05d}", "input": {"q": i}}) + "\n")


def _write_config(tmp_path: Path, dataset_path: Path, runs_dir: Path) -> Path:
    eval_yaml = tmp_path / "eval.yaml"
    eval_yaml.write_text(
        f"""
eval:
  name: perf_10k
dataset:
  type: jsonl
  path: {dataset_path.as_posix()}
systems:
  - name: stub
    adapter: python_function
    target: {_STUB_MODULE}:run
evaluators: []
run:
  max_concurrency: 32
output:
  - type: local_files
    path: {runs_dir.as_posix()}
"""
    )
    return eval_yaml


@pytest.mark.perf
def test_10k_case_run_streams_under_memory_and_time_budgets(
    tmp_path: Path,
) -> None:
    _install_stub()
    dataset_path = tmp_path / "cases.jsonl"
    _write_dataset(dataset_path, _NUM_CASES)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    eval_yaml = _write_config(tmp_path, dataset_path, runs_dir)

    from eval_harness.core.config_loader import load_config
    from eval_harness.runner import build_plan, run_eval

    config = load_config(eval_yaml)

    tracemalloc.start()
    started = time.monotonic()
    try:
        async def main() -> Any:
            plan = await build_plan(config, eval_yaml)
            return await run_eval(plan)

        summary = asyncio.run(main())
        wall_time = time.monotonic() - started
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Sanity: the run actually executed all 10K cells.
    assert summary.cases_total == _NUM_CASES
    assert len(summary.variants) == 1
    assert summary.variants[0].cases_total == _NUM_CASES

    # Memory and time budgets: these are the regression guards. Generous on
    # purpose — the point is to catch `list(outcomes)` style regressions, not
    # to micro-benchmark the runner.
    assert peak < _PEAK_RSS_BUDGET_BYTES, (
        f"peak traced memory {peak / 1024 / 1024:.0f} MiB exceeds budget "
        f"{_PEAK_RSS_BUDGET_BYTES / 1024 / 1024:.0f} MiB"
    )
    assert wall_time < _WALL_TIME_BUDGET_SECONDS, (
        f"wall time {wall_time:.1f}s exceeds budget {_WALL_TIME_BUDGET_SECONDS}s"
    )

    # The aggregated summary was written; confirm the persisted artifact exists.
    summary_path = next(runs_dir.iterdir()) / "summary.yaml"
    assert summary_path.exists(), "summary.yaml not written"
