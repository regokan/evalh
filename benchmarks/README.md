# benchmarks/

Manual capability checks. These are **not** CI gates — they run by hand
when a maintainer wants a real number against a real cluster. The
regression-guard equivalent lives in `tests/perf/`; that one enforces
the local 10K-case wall-time stays within 5% of the v1.x baseline.

## distributed_1m.py

Drives a synthetic 1M-case run through `RayExecutor` against whatever
Ray cluster you point it at. The agent is a no-op so the number you get
back measures eval-harness's dispatch + transport overhead, not adapter
work. Useful for:

- Validating a release against the v2 aspirational target.
- Sanity-checking that a cluster upgrade didn't regress per-cell
  latency.
- Sizing exercises ("how many workers do I need to clear 10M cases in
  an hour?").

### Usage

Local mode (small `--cases`, single-host Ray):

```bash
python benchmarks/distributed_1m.py --cases 10000 --num-workers 16
```

Real cluster (the actual target):

```bash
python benchmarks/distributed_1m.py \
    --cases 1000000 \
    --num-workers 200 \
    --address ray://head-node.cluster:10001 \
    --output-dir /scratch/evalh-1m
```

### What "good" looks like

Numbers from a 200-worker Ray cluster on the v2.0 reference image, no-op
agent, no evaluators (these are reference targets — the script writes a
fresh `benchmark.json` per run so you can compare yours):

| Metric | Target |
|---|---|
| Wall time, 1M cases / 200 workers | ≤ 90 minutes |
| Per-cell median latency | ≤ 50 ms |
| Per-cell p99 latency | ≤ 250 ms |
| Cluster-side failures | < 0.1% (RetryPolicy absorbs the rest) |

Adapter cost dominates as soon as you swap in a real LLM. The
benchmark's wall-time is a floor for what dispatch + transport itself
cost; the real-eval wall-time is `(floor) + adapter_cost`.

### Why not a pytest test?

A 1M-case run is hours and dollars. Folding that under a `pytest`
marker would put cost / time in the wrong category — even gated, it'd
be too tempting to wire into CI by accident. The script-shaped split
also lets the benchmark live next to its `benchmark.json` outputs,
which fall outside the test discovery path.

The 10K perf gate at `tests/perf/test_local_executor_perf.py` IS the
regression guard. Run this script when you want a number, not a pass /
fail.
