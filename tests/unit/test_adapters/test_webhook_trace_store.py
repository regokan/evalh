"""WebhookTraceStore tests.

Slack + Discord exercised via respx mocks (no SDK). Linear exercised
via an injected fake client (the SDK is gated behind the [webhook]
extra; we don't require it for unit tests). Drift-aware formatting is
covered by injecting a `ComparisonReport(kind='drift')` on the
RunSummary and asserting the platform-specific payload.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from eval_harness.adapters.trace.webhook_trace_store import WebhookTraceStore
from eval_harness.core.errors import ConfigError
from eval_harness.core.models import (
    ComparisonReport,
    EvaluationResult,
    RunSummary,
    Trace,
    TraceOutput,
    VariantDelta,
    VariantSummary,
)
from eval_harness.core.time import utc_now

_NOW = datetime(2026, 5, 12, tzinfo=UTC)


@pytest.fixture
def respx_route() -> Iterator[respx.MockRouter]:
    with respx.mock(assert_all_called=False, assert_all_mocked=True) as router:
        yield router


def _summary(
    *,
    run_id: str = "2026-05-12T10-00_demo",
    variants: list[VariantSummary] | None = None,
    comparison: ComparisonReport | None = None,
) -> RunSummary:
    return RunSummary(
        run_id=run_id,
        started_at=_NOW,
        finished_at=_NOW,
        config_path="eval.yaml",
        config_hash="abc",
        cases_total=2,
        variants=variants
        or [
            VariantSummary(
                name="v1",
                cases_total=2,
                cases_passed=2,
                cases_errored=0,
                pass_rate=1.0,
                avg_latency_ms=50.0,
                avg_cost_usd=None,
                avg_tokens_input=None,
                avg_tokens_output=None,
            ),
            VariantSummary(
                name="v2",
                cases_total=2,
                cases_passed=1,
                cases_errored=0,
                pass_rate=0.5,
                avg_latency_ms=80.0,
                avg_cost_usd=None,
                avg_tokens_input=None,
                avg_tokens_output=None,
            ),
        ],
        by_evaluator=[],
        comparison=comparison,
    )


def _drift_comparison(
    *,
    baseline_run_id: str = "run-baseline",
    regressions: list[str] | None = None,
    improvements: list[str] | None = None,
    pass_rate_delta: float = -0.25,
) -> ComparisonReport:
    return ComparisonReport(
        baseline=baseline_run_id,
        deltas=[
            VariantDelta(
                variant="current",
                pass_rate_delta=pass_rate_delta,
                avg_latency_delta_ms=15.0,
                regressions=regressions or ["case_a", "case_b"],
                improvements=improvements or [],
            )
        ],
        kind="drift",
        baseline_run_id=baseline_run_id,
        regressions_count=len(regressions or ["case_a", "case_b"]),
        improvements_count=len(improvements or []),
    )


# ---- Config validation --------------------------------------------------


def test_platform_required() -> None:
    with pytest.raises(ConfigError, match="platform"):
        WebhookTraceStore()


def test_unsupported_platform_rejected() -> None:
    with pytest.raises(ConfigError, match="unsupported"):
        WebhookTraceStore(platform="email", url="x")


def test_slack_url_required() -> None:
    with pytest.raises(ConfigError, match="url"):
        WebhookTraceStore(platform="slack")


def test_discord_url_required() -> None:
    with pytest.raises(ConfigError, match="url"):
        WebhookTraceStore(platform="discord")


def test_linear_api_key_required() -> None:
    with pytest.raises(ConfigError, match="api_key"):
        WebhookTraceStore(platform="linear")


async def test_empty_url_disables_sink_without_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`url: ""` (from `${SLACK_WEBHOOK_URL:-}` with the env var unset)
    must short-circuit cleanly: no HTTP attempt, no ConfigError at plan
    time, no AdapterError at run time — just a logged warning. Lets a
    webhook entry sit in `output:` permanently and only fire when the
    secret is actually present."""
    store = WebhookTraceStore(platform="slack", url="")
    async with store:
        await store.open("r1", Path("/tmp"))
        import logging

        with caplog.at_level(logging.WARNING, logger="eval_harness.webhook"):
            await store.save_summary(_summary())
    assert any("skipping summary POST" in r.message for r in caplog.records)


# ---- URL scheme validation (security) ----------------------------------


def test_slack_rejects_file_scheme_url() -> None:
    """`file://` and other non-http(s) schemes must be rejected — same
    rule as the http SystemAdapter. SSRF defense, no exceptions."""
    with pytest.raises(ConfigError, match="scheme"):
        WebhookTraceStore(platform="slack", url="file:///etc/passwd")


def test_slack_rejects_plain_http_for_non_localhost() -> None:
    """Plain http:// to a non-localhost host is rejected; users must opt
    into https:// for production webhooks."""
    with pytest.raises(ConfigError, match="localhost"):
        WebhookTraceStore(platform="slack", url="http://attacker.example/")


def test_slack_accepts_http_localhost_for_dev() -> None:
    """Dev fixtures need `http://localhost:PORT` — explicitly allowed."""
    store = WebhookTraceStore(
        platform="slack", url="http://localhost:9000/hook"
    )
    assert store.url == "http://localhost:9000/hook"


def test_discord_rejects_gopher_scheme() -> None:
    with pytest.raises(ConfigError, match="scheme"):
        WebhookTraceStore(platform="discord", url="gopher://example.com/")


# ---- Slack --------------------------------------------------------------


async def test_slack_message_includes_pass_rate_per_variant(
    respx_route: respx.MockRouter,
) -> None:
    route = respx_route.post("https://hooks.slack.com/services/AAA/BBB").mock(
        return_value=httpx.Response(200, text="ok")
    )
    store = WebhookTraceStore(
        platform="slack",
        url="https://hooks.slack.com/services/AAA/BBB",
    )
    async with store:
        await store.open("r1", Path("/tmp"))
        await store.save_summary(_summary())

    assert route.call_count == 1
    body = route.calls[0].request.content.decode()
    # Block Kit structure
    assert '"blocks"' in body
    # Both variants surface as fields with pass rate %.
    assert "v1" in body
    assert "v2" in body
    assert "100.0%" in body
    assert "50.0%" in body


async def test_slack_non_200_raises_adapter_error(
    respx_route: respx.MockRouter,
) -> None:
    from eval_harness.core.errors import AdapterError

    respx_route.post("https://hooks.slack.com/x").mock(
        return_value=httpx.Response(500, text="server error")
    )
    store = WebhookTraceStore(platform="slack", url="https://hooks.slack.com/x")
    async with store:
        await store.open("r1", Path("/tmp"))
        with pytest.raises(AdapterError, match="HTTP 500"):
            await store.save_summary(_summary())


# ---- Discord ------------------------------------------------------------


async def test_discord_embed_structure(respx_route: respx.MockRouter) -> None:
    route = respx_route.post("https://discord.com/api/webhooks/AAA/BBB").mock(
        return_value=httpx.Response(204)
    )
    store = WebhookTraceStore(
        platform="discord",
        url="https://discord.com/api/webhooks/AAA/BBB",
    )
    async with store:
        await store.open("r1", Path("/tmp"))
        await store.save_summary(_summary())

    assert route.call_count == 1
    body = route.calls[0].request.content.decode()
    # Discord webhook shape: { "embeds": [{ ... }] }
    assert '"embeds"' in body
    assert "v1" in body
    assert "v2" in body


async def test_discord_default_color_green_for_clean_summary(
    respx_route: respx.MockRouter,
) -> None:
    route = respx_route.post("https://discord.com/x").mock(
        return_value=httpx.Response(204)
    )
    store = WebhookTraceStore(platform="discord", url="https://discord.com/x")
    async with store:
        await store.save_summary(_summary())
    body = route.calls[0].request.content.decode()
    # 0x2ECC71 = 3066993
    assert "3066993" in body


async def test_discord_color_red_when_drift_has_regressions(
    respx_route: respx.MockRouter,
) -> None:
    route = respx_route.post("https://discord.com/x").mock(
        return_value=httpx.Response(204)
    )
    store = WebhookTraceStore(platform="discord", url="https://discord.com/x")
    summary = _summary(comparison=_drift_comparison())
    async with store:
        await store.save_summary(summary)
    body = route.calls[0].request.content.decode()
    # 0xE74C3C = 15158332
    assert "15158332" in body


# ---- Linear (injected fake SDK) ----------------------------------------


class _FakeLinearClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create_comment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {"id": "comment-1"}


async def test_linear_comment_format() -> None:
    fake = _FakeLinearClient()
    store = WebhookTraceStore(
        platform="linear",
        api_key="key",
        issue_id="ENG-123",
        _linear_client=fake,
    )
    async with store:
        await store.open("r1", Path("/tmp"))
        await store.save_summary(_summary())
    assert len(fake.calls) == 1
    body = fake.calls[0]["body"]
    assert "evalh `demo`" in body
    assert "v1" in body
    assert "v2" in body
    # Issue id is propagated for Linear's `issueId` parameter.
    assert fake.calls[0]["issue_id"] == "ENG-123"


# ---- Drift-aware formatting --------------------------------------------


async def test_slack_drift_message_highlights_regressions(
    respx_route: respx.MockRouter,
) -> None:
    route = respx_route.post("https://hooks.slack.com/x").mock(
        return_value=httpx.Response(200, text="ok")
    )
    summary = _summary(
        comparison=_drift_comparison(
            regressions=["case_alpha", "case_beta"],
            improvements=[],
            pass_rate_delta=-0.50,
        )
    )
    store = WebhookTraceStore(platform="slack", url="https://hooks.slack.com/x")
    async with store:
        await store.save_summary(summary)

    body = route.calls[0].request.content.decode()
    assert "Drift" in body
    assert ":warning:" in body
    assert "case_alpha" in body
    assert "case_beta" in body
    assert "-50.00%" in body


async def test_discord_drift_message_includes_regressions(
    respx_route: respx.MockRouter,
) -> None:
    route = respx_route.post("https://discord.com/x").mock(
        return_value=httpx.Response(204)
    )
    summary = _summary(
        comparison=_drift_comparison(regressions=["c1"], improvements=["c2"])
    )
    store = WebhookTraceStore(platform="discord", url="https://discord.com/x")
    async with store:
        await store.save_summary(summary)
    body = route.calls[0].request.content.decode()
    assert "Drift" in body
    assert "c1" in body
    assert "1 regression(s)" in body


async def test_linear_drift_comment_includes_regression_section() -> None:
    fake = _FakeLinearClient()
    summary = _summary(
        comparison=_drift_comparison(regressions=["case_alpha"], improvements=[])
    )
    store = WebhookTraceStore(
        platform="linear",
        api_key="key",
        issue_id="ENG-1",
        _linear_client=fake,
    )
    async with store:
        await store.save_summary(summary)
    body = fake.calls[0]["body"]
    assert "Top regressions" in body
    assert "case_alpha" in body


async def test_non_drift_comparison_does_not_render_drift_section() -> None:
    """`comparison.kind='ad_hoc'` (the v0/v1 default) is NOT a drift report;
    the webhook summary must skip the drift section to avoid surprising
    users with an empty "Drift vs ..." block."""
    fake = _FakeLinearClient()
    ad_hoc = ComparisonReport(
        baseline="v1",
        deltas=[
            VariantDelta(
                variant="v2",
                pass_rate_delta=-0.1,
                avg_latency_delta_ms=5.0,
                regressions=["c1"],
                improvements=[],
            )
        ],
        kind="ad_hoc",
    )
    store = WebhookTraceStore(
        platform="linear",
        api_key="key",
        issue_id="ENG-1",
        _linear_client=fake,
    )
    summary = _summary(comparison=ad_hoc)
    async with store:
        await store.save_summary(summary)
    body = fake.calls[0]["body"]
    assert "Drift vs" not in body
    assert "Top regressions" not in body


# ---- Failure-soft via multi-sink ---------------------------------------


async def test_failure_recorded_to_summary_sink_errors_when_non_first(
    respx_route: respx.MockRouter, tmp_path: Path
) -> None:
    """Headline ev-7aj invariant: a non-first webhook sink that fails on
    save_summary lands its error in `RunSummary.sink_errors` rather than
    aborting the run."""
    from eval_harness.adapters.trace.local_files_store import LocalFilesStore
    from eval_harness.core.config import (
        DatasetConfig,
        EvalConfig,
        EvalIdentity,
        EvaluatorConfig,
        OutputConfig,
        PassCriteria,
        RetryPolicy,
        RunOptions,
        SystemConfig,
    )
    from eval_harness.core.models import EvalCase, RunVariant
    from eval_harness.evaluators.base import Evaluator
    from eval_harness.runner.plan_builder import RunPlan
    from eval_harness.runner.run_eval import run_eval

    respx_route.post("https://hooks.slack.com/x").mock(
        return_value=httpx.Response(500, text="server explodes")
    )

    class _NoopEval(Evaluator):
        type = "noop"

        async def evaluate(self, case, trace, artifact):  # type: ignore[no-untyped-def]
            return EvaluationResult(
                run_id=trace.run_id,
                case_id=case.id,
                variant_name=trace.variant_name,
                evaluator=self.name,
                evaluator_type=self.type,
                passed=True,
                reason="ok",
                started_at=utc_now(),
                finished_at=utc_now(),
                latency_ms=0,
            )

    class _ToyAdapter:
        name = "toy"

        async def __aenter__(self):  # type: ignore[no-untyped-def]
            return self

        async def __aexit__(self, *a):  # type: ignore[no-untyped-def]
            return None

        async def run(self, case, variant, workspace):  # type: ignore[no-untyped-def]
            now = utc_now()
            return Trace(
                run_id="",
                case_id=case.id,
                variant_name=variant.name,
                started_at=now,
                finished_at=now,
                latency_ms=1,
                input=dict(case.input),
                output=TraceOutput(final_answer="ok"),
            )

    local = LocalFilesStore(path=str(tmp_path / "runs"))
    webhook = WebhookTraceStore(
        platform="slack", url="https://hooks.slack.com/x"
    )
    cfg = EvalConfig(
        eval=EvalIdentity(name="webhook-test"),
        dataset=DatasetConfig(type="yaml", path="x"),
        systems=[SystemConfig(name="v1", adapter="fake")],
        evaluators=[EvaluatorConfig(name="ok", type="noop")],
        pass_criteria=PassCriteria(),
        run=RunOptions(max_concurrency=2, retry=RetryPolicy()),
        output=[OutputConfig(type="local_files", path="./runs")],
    )
    plan = RunPlan(
        config=cfg,
        run_id="run-webhook-failure",
        run_dir=tmp_path / "runs" / "run-webhook-failure",
        cases=[EvalCase(id="c1", input={"q": "hi"})],
        variants=[RunVariant(name="v1", adapter="fake", config={})],
        system_adapters={"v1": _ToyAdapter()},
        trace_store=local,
        workspace=None,
        evaluators=[_NoopEval(name="ok")],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
        secondary_trace_stores=[webhook],
    )

    summary = await run_eval(plan)

    # Run completed despite the webhook failure.
    assert summary.variants[0].cases_passed == 1
    # The webhook's save_summary failure landed on sink_errors.
    ops_failed = {e["op"] for e in summary.sink_errors}
    assert "save_summary" in ops_failed
    assert any(
        "AdapterError" in str(e.get("error", "")) for e in summary.sink_errors
    )


# ---- Per-cell hooks are no-ops -----------------------------------------


async def test_per_cell_hooks_are_noops(respx_route: respx.MockRouter) -> None:
    """save_trace / save_evaluation / save_artifact must NOT hit the webhook
    — webhook reporting is summary-grained."""
    route = respx_route.post("https://hooks.slack.com/x").mock(
        return_value=httpx.Response(200, text="ok")
    )
    store = WebhookTraceStore(platform="slack", url="https://hooks.slack.com/x")
    async with store:
        await store.open("r1", Path("/tmp"))
        await store.save_trace(
            Trace(
                run_id="r1",
                case_id="c1",
                variant_name="v1",
                started_at=_NOW,
                finished_at=_NOW,
                latency_ms=0,
                input={},
                output=TraceOutput(final_answer="x"),
            )
        )
        await store.save_evaluation("c1", "v1", [])
    # No POST happened — webhook is summary-grained.
    assert route.call_count == 0


# ---- Read methods are write-only ---------------------------------------


async def test_iter_methods_return_empty() -> None:
    store = WebhookTraceStore(platform="slack", url="https://hooks.slack.com/x")
    async with store:
        traces = [t async for t in store.iter_traces()]
        results = [r async for r in store.iter_results()]
        assert traces == []
        assert results == []
        assert await store.load_summary("r1") is None
        assert await store.list_run_ids() == []


# ---- Factory registration ----------------------------------------------


def test_factory_registers_webhook() -> None:
    from eval_harness.factories import trace_store_factory

    trace_store_factory.load_entry_points()
    assert "webhook" in trace_store_factory.registry.names()
