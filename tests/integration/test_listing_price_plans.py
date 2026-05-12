from __future__ import annotations

from pathlib import Path
from typing import Any

from eval_harness.adapters.system.base import SystemAdapter
from eval_harness.core.config_loader import load_config
from eval_harness.runner import build_plan

_LISTING_PRICE = (
    Path(__file__).resolve().parents[2] / "examples" / "listing_price" / "eval.yaml"
)


async def test_listing_price_plan_builds_without_running(
    tmp_path: Path,
    monkeypatch: Any,
    fake_judge_backend: None,
) -> None:
    monkeypatch.setenv("AGENT_API_KEY", "dummy-test-key")

    config = load_config(_LISTING_PRICE)
    config.output[0].path = str(tmp_path / "runs")
    config.dataset.path = str(_LISTING_PRICE.parent / "cases.yaml")

    plan = await build_plan(config, _LISTING_PRICE)

    # Both variants resolve to a runtime SystemAdapter.
    assert {v.name for v in plan.variants} == {"agent_main", "agent_experimental"}
    for variant in plan.variants:
        adapter = plan.system_adapters[variant.name]
        assert isinstance(adapter, SystemAdapter)

    # All cases were loaded from cases.yaml.
    assert len(plan.cases) > 0
    assert all(case.id.startswith("listing_price_") for case in plan.cases)

    # baseline_variant routes through and is well-formed.
    assert plan.baseline_variant == "agent_main"
