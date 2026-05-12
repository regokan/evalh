"""v1-supplement done-when integration tests.

Wires the runner end-to-end against fake observability platforms — no real
network, no API keys. Specifically:

  - Fixture DatasetAdapter -> replay SystemAdapter (the online-eval pair
    that proves Langfuse / Phoenix DatasetAdapters could slot in
    unchanged once installed).
  - Multi-sink output: a fake OTel collector receives every span with
    `run_id` + `case_id` attributes alongside the canonical
    `local_files` sink.
  - `RunSummary.sink_errors` captures a failing non-first sink without
    aborting the run.

Skips when the [otel] extra isn't installed.
"""

from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any, Self

import pytest

pytest.importorskip("opentelemetry.sdk.trace")
pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from eval_harness._platforms import otel as otel_mod
from eval_harness._platforms.otel import OtelClient
from eval_harness.adapters.trace.local_files_store import LocalFilesStore
from eval_harness.adapters.trace.otel_trace_store import OtelTraceStore
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
from eval_harness.core.models import (
    EvalCase,
    EvaluationResult,
    FilesystemArtifact,
    RunSummary,
    RunVariant,
    Trace,
    TraceOutput,
)
from eval_harness.core.time import utc_now
from eval_harness.evaluators.base import Evaluator
from eval_harness.runner.plan_builder import RunPlan
from eval_harness.runner.run_eval import run_eval


@pytest.fixture(autouse=True)
def _clean_registries() -> None:
    otel_mod._clear_registry_for_tests()


def _make_in_memory_otel() -> tuple[OtelClient, InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider(resource=Resource.create({"service.name": "evalh-test"}))
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    client = OtelClient(endpoint="http://test:4318/v1/traces")
    client._provider = provider
    return client, exporter


class _ReplayAdapter:
    """Tiny inline replay adapter — unwraps `_embedded_trace` from each
    case so the test doesn't need to spin up the full fixture dataset +
    replay adapter pair. The contract is the same."""

    def __init__(self) -> None:
        self.name = "replay"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def run(
        self, case: EvalCase, variant: RunVariant, workspace: Any
    ) -> Trace:
        embedded = case._embedded_trace
        if embedded is None:
            raise RuntimeError(f"case {case.id} has no _embedded_trace")
        now = utc_now()
        return Trace(
            run_id="",
            case_id=case.id,
            variant_name=variant.name,
            started_at=embedded.started_at,
            finished_at=embedded.finished_at,
            latency_ms=embedded.latency_ms,
            input=dict(case.input),
            output=embedded.output,
            tool_calls=list(embedded.tool_calls),
            metrics=embedded.metrics,
            extra={"source": "replay", **embedded.extra, "replayed_at": now.isoformat()},
        )


class _PassEvaluator(Evaluator):
    type = "pass_always"

    async def evaluate(
        self,
        case: EvalCase,
        trace: Trace,
        artifact: FilesystemArtifact | None,
    ) -> EvaluationResult:
        now = utc_now()
        return EvaluationResult(
            run_id=trace.run_id,
            case_id=case.id,
            variant_name=trace.variant_name,
            evaluator=self.name,
            evaluator_type=self.type,
            passed=True,
            reason="ok",
            started_at=now,
            finished_at=now,
            latency_ms=0,
        )


def _embedded_case(case_id: str, answer: str) -> EvalCase:
    """Fixture-platform style case: an EvalCase with `_embedded_trace`
    pre-populated. Matches what fixture / langfuse / phoenix DatasetAdapters
    produce in `embed_full_trace: true` mode."""
    now = utc_now()
    case = EvalCase(
        id=case_id,
        input={"user_message": f"q-{case_id}"},
        metadata={"source": "fake-platform"},
    )
    case._embedded_trace = Trace(
        run_id="prod",
        case_id=case_id,
        variant_name="production",
        started_at=now,
        finished_at=now,
        latency_ms=12,
        input={"user_message": f"q-{case_id}"},
        output=TraceOutput(final_answer=answer),
        extra={"trace_id": f"upstream-{case_id}", "source_platform": "fake"},
    )
    return case


def _config() -> EvalConfig:
    return EvalConfig(
        eval=EvalIdentity(name="v1s-e2e"),
        dataset=DatasetConfig(type="fixture", path="ignored-by-the-stub"),
        systems=[SystemConfig(name="replay", adapter="replay")],
        evaluators=[EvaluatorConfig(name="ok", type="pass_always")],
        pass_criteria=PassCriteria(),
        run=RunOptions(max_concurrency=4, retry=RetryPolicy()),
        output=[OutputConfig(type="local_files", path="./runs")],
    )


async def test_fixture_platform_end_to_end_through_replay_and_otel(
    tmp_path: Path,
) -> None:
    """The v1-supplement done-when: a fake observability platform's cases
    flow through the replay SystemAdapter end-to-end, and the OTel
    secondary sink receives every span with `run_id` + `case_id`
    attributes alongside the canonical local_files sink.

    Proves the same shape Langfuse/Phoenix DatasetAdapters slot into."""
    cases = [_embedded_case("c1", "Richmond"), _embedded_case("c2", "Brunswick")]
    otel_client, otel_exporter = _make_in_memory_otel()
    otel_store = OtelTraceStore(
        endpoint="http://test:4318/v1/traces", _client=otel_client
    )
    local_store = LocalFilesStore(path=str(tmp_path / "runs"))

    cfg = _config()
    plan = RunPlan(
        config=cfg,
        run_id="run-v1s-e2e",
        run_dir=tmp_path / "runs" / "run-v1s-e2e",
        cases=cases,
        variants=[RunVariant(name="replay", adapter="replay", config={})],
        system_adapters={"replay": _ReplayAdapter()},
        trace_store=local_store,
        workspace=None,
        evaluators=[_PassEvaluator(name="ok")],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
        secondary_trace_stores=[otel_store],
    )

    summary = await run_eval(plan)

    # Canonical sink wrote the run on disk.
    assert (plan.run_dir / "traces.jsonl").exists()
    assert (plan.run_dir / "summary.yaml").exists()

    # OTel secondary sink received spans for every (case, variant), and
    # every emitted span carries run_id + case_id attributes that downstream
    # backends use to group spans.
    spans = otel_exporter.get_finished_spans()
    trace_spans = [s for s in spans if s.name.startswith("trace:")]
    assert {s.name for s in trace_spans} == {"trace:c1", "trace:c2"}
    for s in trace_spans:
        attrs = dict(s.attributes or {})
        assert attrs["evalh.run_id"] == "run-v1s-e2e"
        assert attrs["evalh.case_id"] in {"c1", "c2"}
    # The run-summary span lands on the OTel sink too.
    assert any(s.name.startswith("run_summary:") for s in spans)

    # Summary's sink_errors list is empty when every sink succeeded.
    assert summary.sink_errors == []


# ---- Multi-sink failure-soft -------------------------------------------


class _FailingSink:
    """Secondary sink that raises on every save_*. The runner must
    capture the failures into `RunSummary.sink_errors` and continue."""

    rendered_config: dict[str, Any] | None = None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def open(self, run_id: str, run_dir: Path) -> None:
        return None

    async def save_trace(self, trace: Trace) -> None:
        raise RuntimeError("secondary save_trace failed")

    async def save_evaluation(
        self, case_id: str, variant: str, results: list[EvaluationResult]
    ) -> None:
        raise RuntimeError("secondary save_evaluation failed")

    async def save_artifact(self, artifact: FilesystemArtifact) -> None:
        return None

    async def save_summary(self, summary: RunSummary) -> None:
        raise RuntimeError("secondary save_summary failed")

    async def close(self) -> None:
        return None


async def test_failing_secondary_sink_does_not_abort_run(tmp_path: Path) -> None:
    """The headline multi-sink invariant. The canonical sink wins; the
    failing mirror lands its exceptions in `summary.sink_errors`."""
    cases = [_embedded_case("c1", "Richmond")]
    cfg = _config()
    local_store = LocalFilesStore(path=str(tmp_path / "runs"))
    plan = RunPlan(
        config=cfg,
        run_id="run-sink-failure",
        run_dir=tmp_path / "runs" / "run-sink-failure",
        cases=cases,
        variants=[RunVariant(name="replay", adapter="replay", config={})],
        system_adapters={"replay": _ReplayAdapter()},
        trace_store=local_store,
        workspace=None,
        evaluators=[_PassEvaluator(name="ok")],
        retry_policy=cfg.run.retry,
        baseline_variant=None,
        secondary_trace_stores=[_FailingSink()],
    )

    summary = await run_eval(plan)

    # Run completed cleanly on the canonical sink.
    assert (plan.run_dir / "traces.jsonl").exists()
    # Secondary failures were captured rather than raised.
    sinks_failed = {e["op"] for e in summary.sink_errors}
    assert "save_trace" in sinks_failed
    assert "save_summary" in sinks_failed
    # Cell still passed end-to-end.
    assert summary.variants[0].cases_passed == 1
