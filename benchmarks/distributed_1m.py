"""Aspirational 1M-case Ray benchmark for the v2 closing milestone (ev-k4p).

This is a MAINTAINER script, **not** a CI gate. Run it by hand against a
real Ray cluster when you want a number for "what does eval-harness do
at one-million-case scale?". The CI suite enforces the 10K perf gate
(see ``tests/perf/test_local_executor_perf.py``); the 1M target is a
documented capability check, not a regression alarm.

Inputs:

- ``--cases``: total synthetic case count (default 1_000_000). Each case
  is trivially cheap on the worker side — the bench measures dispatch
  + transport overhead, not adapter work.
- ``--num-workers``: how many parallel cells Ray runs at once. The
  aspirational target was a 200-worker cluster; tune to your actual
  cluster.
- ``--address``: Ray cluster address. ``auto`` attaches to a running
  cluster; ``None`` (the default) spins up a local cluster, which is
  enough to drive a few thousand cases on a beefy laptop but won't
  reach 1M in any reasonable wall-clock.
- ``--output-dir``: where to drop the run dir + the JSON metrics blob.

Outputs:

A ``runs/<run_id>/`` directory with the usual ``traces.jsonl`` /
``results.jsonl`` / ``summary.yaml``, plus ``benchmark.json`` next to it
carrying wall-time, per-cell latency (mean / p50 / p99), and reported
cost. Compare against the numbers documented in ``benchmarks/README.md``
to see whether your cluster + your version of eval-harness still hit
the envelope.

Why a script rather than a pytest test:

A 1M-case run takes hours and burns real cluster CPU. Folding that into
``pytest`` (even gated by a marker) puts the cost / time profile in the
wrong category. The right shape is "I'm validating a release; let me
spin up a cluster and run this script."
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any


def _build_dataset(path: Path, n: int) -> None:
    """Synthesize a JSONL dataset of ``n`` trivial cases. The bench
    measures executor overhead, not adapter cost — keep cases tiny so
    Ray's transport layer is the bottleneck, not the agent."""
    with path.open("w", encoding="utf-8") as f:
        for i in range(n):
            f.write(json.dumps({"id": f"c{i:07d}", "input": {"q": i}}) + "\n")


def _write_eval_yaml(
    config_path: Path,
    *,
    dataset_path: Path,
    runs_dir: Path,
    address: str | None,
    num_workers: int,
) -> None:
    address_block = ""
    if address is not None:
        address_block = f"      address: {address}\n"
    config_path.write_text(
        f"""schema_version: "1.0"
eval:
  name: distributed_1m
dataset:
  type: jsonl
  path: {dataset_path.as_posix()}
systems:
  - name: noop
    adapter: python_function
    target: benchmarks.distributed_1m:agent
evaluators: []
run:
  max_concurrency: {num_workers}
  executor:
    type: ray
    config:
{address_block}      num_cpus_per_cell: 1
output:
  - type: local_files
    path: {runs_dir.as_posix()}
"""
    )


def agent(case: dict[str, Any], variant: dict[str, Any]) -> dict[str, Any]:
    """Trivially-cheap stub. The point of the benchmark is to time the
    executor, not the adapter; this function returns in microseconds."""
    return {
        "final_answer": "ok",
        "metrics": {"token_input": 1, "token_output": 1, "cost_usd": 0.0},
    }


async def _drive(config_path: Path) -> dict[str, Any]:
    from eval_harness.core.config import load_eval_config
    from eval_harness.runner import build_plan, run_eval

    config = load_eval_config(config_path)
    plan = await build_plan(config, config_path)
    started = time.perf_counter()
    summary = await run_eval(plan)
    elapsed = time.perf_counter() - started
    per_cell = [
        # Re-read the persisted traces for latency stats — the in-memory
        # aggregator only carries variant-level rollups.
        t.latency_ms
        for t in []  # populated below from disk
    ]
    # Read traces.jsonl off disk for the latency distribution.
    traces_path = plan.run_dir / "traces.jsonl"
    if traces_path.exists():
        with traces_path.open(encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                trace = json.loads(line)
                per_cell.append(int(trace.get("latency_ms", 0)))
    return {
        "run_id": summary.run_id,
        "run_dir": str(plan.run_dir),
        "wall_time_seconds": elapsed,
        "cases_total": summary.cases_total,
        "per_cell_latency_ms": {
            "count": len(per_cell),
            "mean": statistics.fmean(per_cell) if per_cell else 0.0,
            "p50": statistics.median(per_cell) if per_cell else 0.0,
            "p99": (
                statistics.quantiles(per_cell, n=100)[98]
                if len(per_cell) >= 100
                else 0.0
            ),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=1_000_000)
    parser.add_argument("--num-workers", type=int, default=200)
    parser.add_argument(
        "--address",
        type=str,
        default=None,
        help="Ray cluster address (e.g. 'ray://head-node:10001'). Omit for local-only.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("benchmark_runs"),
        help="Where the run dir + metrics JSON land.",
    )
    args = parser.parse_args(argv)

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    dataset_path = output_dir / "cases.jsonl"
    config_path = output_dir / "eval.yaml"
    _build_dataset(dataset_path, args.cases)
    _write_eval_yaml(
        config_path,
        dataset_path=dataset_path,
        runs_dir=output_dir / "runs",
        address=args.address,
        num_workers=args.num_workers,
    )

    metrics = asyncio.run(_drive(config_path))
    metrics_path = output_dir / "benchmark.json"
    metrics_path.write_text(json.dumps(metrics, indent=2))

    print(f"wall_time={metrics['wall_time_seconds']:.2f}s cases={metrics['cases_total']}")
    print(f"per-cell mean={metrics['per_cell_latency_ms']['mean']:.1f}ms "
          f"p50={metrics['per_cell_latency_ms']['p50']:.1f}ms "
          f"p99={metrics['per_cell_latency_ms']['p99']:.1f}ms")
    print(f"metrics: {metrics_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
