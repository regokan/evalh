"""TraceEnricher Protocol + factory tests.

The runner-level failure-soft and chaining behaviour lives in
`tests/unit/test_runner_enrichment.py`. This file covers the Protocol /
factory plumbing.
"""

from __future__ import annotations

import pytest

from eval_harness.adapters.enricher import TraceEnricher
from eval_harness.adapters.enricher.base import TraceEnricher as TraceEnricherProto
from eval_harness.core.errors import ConfigError
from eval_harness.core.models import Trace, TraceOutput
from eval_harness.core.time import utc_now
from eval_harness.factories import trace_enricher_factory
from tests.fixtures.enrichers.fake_enricher import FakeEnricher


def test_module_reexports_protocol() -> None:
    """`eval_harness.adapters.enricher` re-exports the Protocol so callers
    don't reach into `base`."""
    assert TraceEnricher is TraceEnricherProto


def test_fake_enricher_satisfies_protocol_runtime_check() -> None:
    fake = FakeEnricher(name="fake")
    assert isinstance(fake, TraceEnricher)


def test_factory_unknown_type_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="trace_enricher"):
        trace_enricher_factory.build({"type": "definitely-not-registered"})


def test_factory_missing_type_raises_config_error() -> None:
    with pytest.raises(ConfigError, match="'type'"):
        trace_enricher_factory.build({})


def test_factory_builds_registered_enricher() -> None:
    prior = trace_enricher_factory.registry._items.get("_fake")
    trace_enricher_factory.register("_fake", FakeEnricher)
    try:
        built = trace_enricher_factory.build(
            {"type": "_fake", "name": "from-factory", "delay_ticks": 2}
        )
        assert isinstance(built, FakeEnricher)
        assert built.name == "from-factory"
        assert built.delay_ticks == 2
    finally:
        if prior is None:
            trace_enricher_factory.registry._items.pop("_fake", None)
        else:
            trace_enricher_factory.registry._items["_fake"] = prior


def test_entry_point_group_declared() -> None:
    """The `eval_harness.trace_enrichers` group must be discoverable even
    though no built-in enrichers register yet — concrete observability
    enrichers will register here in v1-supplement."""
    from importlib.metadata import entry_points

    # `entry_points(group=...)` returns an empty selection if no entries
    # are registered. The runtime call is what matters.
    eps = entry_points(group="eval_harness.trace_enrichers")
    assert eps is not None
    # Smoke: the factory's load is idempotent regardless of whether the
    # group is populated.
    trace_enricher_factory.load_entry_points()
    trace_enricher_factory.load_entry_points()


async def test_fake_enricher_lifecycle_and_enrichment() -> None:
    fake = FakeEnricher(name="ann", enriched_fields={"langfuse_id": "abc"})
    now = utc_now()
    trace = Trace(
        run_id="r1",
        case_id="c1",
        variant_name="v1",
        started_at=now,
        finished_at=now,
        latency_ms=0,
        input={},
        output=TraceOutput(final_answer="x"),
    )
    async with fake:
        out = await fake.enrich(trace)
    assert fake.entered and fake.exited
    assert fake.call_count == 1
    assert out.extra["enriched_by"] == ["ann"]
    assert out.extra["enrichment"] == {"langfuse_id": "abc"}
