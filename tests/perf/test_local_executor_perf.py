"""v2 LocalExecutor perf gate.

Re-runs the v0.2 10K-case fixture against the v2 runner + LocalExecutor
and asserts wall-time stays within 5% of the pre-v2 baseline captured
in `tests/perf/baselines/local_executor_10k.json`. The bead's load-
bearing assert: wrapping the existing asyncio.gather + Semaphore under
the Protocol cannot add measurable overhead per cell.

This is a *guard*, not a benchmark. Tolerance is intentionally
generous (5%) so machine-to-machine variation doesn't flake the test;
a real regression (someone re-introducing the per-cell `submit_cell`
loop on the local path, say) blows it by a lot more than 5%.

Default pytest invocations exclude this with `-m "not perf"`.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import types
from pathlib import Path
from typing import Any

import pytest

_STUB_MODULE = "_evalh_perf_stub_agent_v2"
_NUM_CASES = 10_000
_BASELINE_PATH = Path(__file__).parent / "baselines" / "local_executor_10k.json"


def _install_stub() -> None:
    def agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
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
  name: perf_local_executor_10k
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
def test_v2_local_executor_within_5pct_of_pre_v2_baseline(
    tmp_path: Path,
) -> None:
    """Headline ev-0zt perf gate. v2 wall-time on the 10K-case fixture
    stays within 5% of the pre-v2 baseline."""
    _install_stub()
    dataset_path = tmp_path / "cases.jsonl"
    _write_dataset(dataset_path, _NUM_CASES)
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    eval_yaml = _write_config(tmp_path, dataset_path, runs_dir)

    baseline = json.loads(_BASELINE_PATH.read_text())
    budget = baseline["wall_time_seconds"] * (1.0 + baseline["tolerance_pct"] / 100.0)

    from eval_harness.core.config_loader import load_config
    from eval_harness.runner import build_plan, run_eval

    config = load_config(eval_yaml)

    started = time.monotonic()

    async def main() -> Any:
        plan = await build_plan(config, eval_yaml)
        return await run_eval(plan)

    summary = asyncio.run(main())
    wall_time = time.monotonic() - started

    # Sanity: the run actually executed all 10K cells via LocalExecutor.
    assert summary.cases_total == _NUM_CASES
    assert summary.variants[0].cases_total == _NUM_CASES

    assert wall_time < budget, (
        f"v2 wall_time {wall_time:.2f}s exceeds 5%-tolerance budget "
        f"{budget:.2f}s (pre-v2 baseline {baseline['wall_time_seconds']:.2f}s). "
        f"Likely cause: `LocalExecutor.dispatch_all` no longer uses a "
        f"single bulk `asyncio.gather` (someone reintroduced a per-cell "
        f"`submit_cell + await` round-trip on the in-process path)."
    )
