from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from eval_harness.core.models import EvaluationResult


@click.command("re-evaluate")
@click.argument(
    "run_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--add",
    "evaluator_name",
    default=None,
    help=(
        "Re-run only the named evaluator (must exist in the original "
        "eval.yaml's evaluators[]). Default: re-run all."
    ),
)
def re_evaluate(run_dir: Path, evaluator_name: str | None) -> None:
    """Re-score traces in <run_dir> against the original config's evaluators.

    Offline — no system calls. Reads traces.jsonl, runs the (deterministic
    or stochastic) evaluators again, and APPENDS to results.jsonl.
    """
    # Heavy imports deferred so `evalh --help` stays fast.
    from eval_harness.core.errors import ConfigError

    try:
        appended = asyncio.run(_main(run_dir, evaluator_name))
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"appended {appended} result(s) to {run_dir / 'results.jsonl'}")


async def _main(run_dir: Path, evaluator_name: str | None) -> int:
    from eval_harness.core.config import EvalConfig
    from eval_harness.core.errors import ConfigError
    from eval_harness.core.models import EvalCase, RunVariant, Trace
    from eval_harness.core.run_reader import RunReader
    from eval_harness.evaluators.base import Evaluator
    from eval_harness.factories.dataset_adapter_factory import DatasetAdapterFactory
    from eval_harness.factories.evaluator_factory import EvaluatorFactory
    from eval_harness.runner.run_eval import _normalize_result

    reader = RunReader(run_dir)
    config = EvalConfig.model_validate(await reader.load_config())

    evaluator_configs = list(config.evaluators)
    if evaluator_name is not None:
        evaluator_configs = [e for e in evaluator_configs if e.name == evaluator_name]
        if not evaluator_configs:
            raise ConfigError(
                f"re-evaluate: --add '{evaluator_name}' did not match any "
                f"evaluator in the run's config. Known: "
                f"{[e.name for e in config.evaluators]}"
            )

    evaluator_factory = EvaluatorFactory()
    evaluator_factory.load_entry_points()
    evaluators: list[Evaluator] = [evaluator_factory.build(e) for e in evaluator_configs]

    dataset_factory = DatasetAdapterFactory()
    dataset_factory.load_entry_points()
    dataset_adapter = dataset_factory.build(config.dataset.model_dump())
    try:
        loaded = await dataset_adapter.load_cases()
    except (ConfigError, FileNotFoundError) as e:
        raise ConfigError(
            f"re-evaluate: could not load dataset from "
            f"{config.dataset.path!r}: {e}. The original dataset must still be "
            f"reachable from {run_dir}."
        ) from e
    cases_by_id: dict[str, EvalCase] = {case.id: case for case in loaded}

    results_path = run_dir / "results.jsonl"
    appended = 0
    async for trace in reader.iter_traces():
        assert isinstance(trace, Trace)
        case = cases_by_id.get(trace.case_id)
        if case is None:
            raise ConfigError(
                f"re-evaluate: trace references case_id '{trace.case_id}' "
                f"which is not in the current dataset"
            )
        raw_results = await asyncio.gather(
            *[ev.evaluate(case, trace, None) for ev in evaluators],
            return_exceptions=True,
        )
        variant = RunVariant(name=trace.variant_name, adapter="<replay>", config={})
        results = [
            _normalize_result(r, ev, trace.run_id, case, variant)
            for r, ev in zip(raw_results, evaluators, strict=True)
        ]
        await asyncio.to_thread(_append_results, results_path, results)
        appended += len(results)

    return appended


def _append_results(path: Path, results: list[EvaluationResult]) -> None:
    payload = "".join(r.model_dump_json() + "\n" for r in results)
    with path.open("a", encoding="utf-8") as f:
        f.write(payload)
