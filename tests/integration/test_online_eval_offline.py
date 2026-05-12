"""Integration: `examples/online_eval/` runs end-to-end OFFLINE.

The roadmap promise for v1 is that online-evaluation (replay against
historical traces) works with no network, no API keys — the
`fixture` DatasetAdapter + `replay` SystemAdapter + deterministic
evaluators are the offline pair that proves the contract.

We don't shell out to the CLI; we run the same pipeline the CLI runs.
That keeps the test fast and the failure messages legible.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eval_harness.core.config_loader import load_config
from eval_harness.runner import build_plan, run_eval

_EVAL = Path(__file__).resolve().parents[2] / "examples" / "online_eval" / "eval.yaml"


@pytest.mark.integration
async def test_online_eval_runs_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped fixture + replay pipeline produces a summary with one
    variant (production_replay), the expected case count, and at least one
    passing cell — without ever hitting the network."""
    monkeypatch.chdir(_EVAL.parent.parent.parent)
    config = load_config(_EVAL)
    # Re-target the output to a temp dir so we don't leave runs/<...>/ behind.
    config.output[0].path = str(tmp_path)
    plan = await build_plan(config, _EVAL)
    summary = await run_eval(plan)

    assert summary.cases_total > 0
    variant_names = {v.name for v in summary.variants}
    assert "production_replay" in variant_names

    replay = next(v for v in summary.variants if v.name == "production_replay")
    assert replay.cases_total == summary.cases_total
    # The fixture is sized so at least one case passes the evaluator chain.
    assert replay.cases_passed >= 1
    # And no case errors out — replay against a clean fixture should be
    # deterministic (no platform calls, no LLM calls).
    assert replay.cases_errored == 0
