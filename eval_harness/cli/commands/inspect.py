from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from rich.console import Console

    from eval_harness.core.models import (
        EvaluationResult,
        FilesystemArtifact,
        RunSummary,
        Trace,
    )

_THINKING_TRUNCATE = 2048
_DIFF_LINE_TRUNCATE = 200


@click.command("inspect")
@click.argument(
    "run_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--case",
    "case_id",
    default=None,
    help="Filter to a specific case_id.",
)
@click.option(
    "--variant",
    "variant_name",
    default=None,
    help="Filter to a specific variant name.",
)
@click.option(
    "--failed",
    is_flag=True,
    default=False,
    help="Show only cells that didn't pass.",
)
@click.option(
    "--no-truncate",
    is_flag=True,
    default=False,
    help="Don't truncate long thinking blocks.",
)
@click.option(
    "--no-artifacts",
    is_flag=True,
    default=False,
    help="Skip rendering FilesystemArtifact even when present.",
)
def inspect(
    run_dir: Path,
    case_id: str | None,
    variant_name: str | None,
    failed: bool,
    no_truncate: bool,
    no_artifacts: bool,
) -> None:
    """Inspect a finished eval run.

    With no flags, prints the per-variant summary and a table of all
    (case, variant) cells. Use --case for per-cell detail (input, output,
    thinking, tool_calls, messages, metrics, evaluator results).
    """
    from eval_harness.core.errors import ConfigError

    try:
        asyncio.run(
            _inspect(
                run_dir, case_id, variant_name, failed, no_truncate, no_artifacts
            )
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc


async def _inspect(
    run_dir: Path,
    case_id: str | None,
    variant_name: str | None,
    failed: bool,
    no_truncate: bool,
    no_artifacts: bool,
) -> None:
    from rich.console import Console

    from eval_harness.core.run_reader import RunReader

    reader = RunReader(run_dir)
    summary = await reader.load_summary()

    traces: list[Trace] = []
    async for t in reader.iter_traces():
        if case_id is not None and t.case_id != case_id:
            continue
        if variant_name is not None and t.variant_name != variant_name:
            continue
        traces.append(t)

    results_by_cell: dict[tuple[str, str], list[EvaluationResult]] = {}
    async for r in reader.iter_results():
        results_by_cell.setdefault((r.case_id, r.variant_name), []).append(r)

    console = Console()

    if failed:
        traces = [
            t
            for t in traces
            if not _cell_passed(t, results_by_cell.get((t.case_id, t.variant_name), []))
        ]

    if not traces:
        if failed:
            console.print("[green]no failures match the filters[/]")
        else:
            console.print("[yellow]no traces match the filters[/]")
        return

    if case_id is None and variant_name is None and not failed:
        _print_variant_summary(console, summary)

    if case_id is not None:
        for t in traces:
            results = results_by_cell.get((t.case_id, t.variant_name), [])
            _print_cell_detail(console, t, results, truncate=not no_truncate)
            if not no_artifacts:
                artifact = _load_artifact(run_dir, t.case_id, t.variant_name)
                if artifact is not None:
                    _print_artifact(console, artifact, truncate=not no_truncate)
    else:
        _print_cell_table(console, traces, results_by_cell)


def _cell_passed(trace: Trace, results: list[EvaluationResult]) -> bool:
    if trace.error is not None:
        return False
    if not results:
        return True
    return all(r.passed for r in results)


def _print_variant_summary(console: Console, summary: RunSummary) -> None:
    from rich.table import Table

    console.print(f"[bold]run_id[/]: {summary.run_id}")
    console.print(f"[bold]config[/]: {summary.config_path}")
    console.print(f"[bold]cases_total[/]: {summary.cases_total}")

    table = Table(title="Per-variant summary")
    table.add_column("variant")
    table.add_column("passed", justify="right")
    table.add_column("errored", justify="right")
    table.add_column("pass_rate", justify="right")
    table.add_column("avg_latency_ms", justify="right")
    for v in summary.variants:
        table.add_row(
            v.name,
            f"{v.cases_passed}/{v.cases_total}",
            str(v.cases_errored),
            f"{v.pass_rate:.2%}",
            f"{v.avg_latency_ms:.0f}",
        )
    console.print(table)


def _print_cell_table(
    console: Console,
    traces: list[Trace],
    results_by_cell: dict[tuple[str, str], list[EvaluationResult]],
) -> None:
    from rich.table import Table

    table = Table(title="Cells")
    table.add_column("case_id")
    table.add_column("variant")
    table.add_column("passed")
    table.add_column("score", justify="right")
    table.add_column("latency_ms", justify="right")
    for t in traces:
        results = results_by_cell.get((t.case_id, t.variant_name), [])
        passed = _cell_passed(t, results)
        score = _avg_score(results)
        if t.error is not None:
            status = "[red]ERROR[/]"
        elif passed:
            status = "[green]pass[/]"
        else:
            status = "[red]fail[/]"
        table.add_row(
            t.case_id,
            t.variant_name,
            status,
            "—" if score is None else f"{score:.2f}",
            f"{t.latency_ms}",
        )
    console.print(table)


def _avg_score(results: list[EvaluationResult]) -> float | None:
    scored = [r.score for r in results if r.score is not None]
    if not scored:
        return None
    return sum(scored) / len(scored)


def _print_cell_detail(
    console: Console,
    trace: Trace,
    results: list[EvaluationResult],
    *,
    truncate: bool,
) -> None:
    from rich.panel import Panel
    from rich.table import Table

    title = f"[bold]case[/] `{trace.case_id}` x [bold]variant[/] `{trace.variant_name}`"
    console.rule(title)

    console.print(f"[bold]latency_ms[/]: {trace.latency_ms}")
    if trace.error is not None:
        console.print(
            Panel(
                f"[red]{trace.error.type}[/]: {trace.error.message}",
                title="error",
                border_style="red",
            )
        )

    console.print(Panel(_format_json(trace.input), title="input", border_style="cyan"))

    final = trace.output.final_answer or ""
    console.print(
        Panel(final or "[dim](empty)[/]", title="output.final_answer", border_style="green")
    )

    if trace.output.thinking:
        thinking = trace.output.thinking
        suffix = ""
        if truncate and len(thinking) > _THINKING_TRUNCATE:
            thinking = thinking[:_THINKING_TRUNCATE]
            suffix = (
                f"\n\n[dim]… truncated ({len(trace.output.thinking) - _THINKING_TRUNCATE} "
                f"chars). Pass --no-truncate to see full thinking.[/]"
            )
        console.print(
            Panel(thinking + suffix, title="output.thinking", border_style="magenta")
        )

    if trace.tool_calls:
        console.print(
            Panel(
                _format_json([c.model_dump(mode="json") for c in trace.tool_calls]),
                title=f"tool_calls ({len(trace.tool_calls)})",
                border_style="yellow",
            )
        )

    if trace.tool_results:
        console.print(
            Panel(
                _format_json([r.model_dump(mode="json") for r in trace.tool_results]),
                title=f"tool_results ({len(trace.tool_results)})",
                border_style="yellow",
            )
        )

    if trace.messages:
        console.print(
            Panel(
                _format_json([m.model_dump(mode="json") for m in trace.messages]),
                title=f"messages ({len(trace.messages)})",
                border_style="blue",
            )
        )

    metrics = trace.metrics.model_dump(mode="json", exclude_none=True)
    if metrics:
        console.print(Panel(_format_json(metrics), title="metrics", border_style="cyan"))

    if results:
        table = Table(title="evaluator results")
        table.add_column("evaluator")
        table.add_column("type")
        table.add_column("passed")
        table.add_column("score", justify="right")
        table.add_column("reason")
        for r in results:
            passed_str = (
                "[red]ERROR[/]" if r.error is not None
                else "[green]pass[/]" if r.passed
                else "[red]fail[/]"
            )
            score_str = "—" if r.score is None else f"{r.score:.2f}"
            reason = r.reason
            if len(reason) > 100:
                reason = reason[:97] + "..."
            table.add_row(r.evaluator, r.evaluator_type, passed_str, score_str, reason)
        console.print(table)


def _format_json(value: object) -> str:
    try:
        return json.dumps(value, indent=2, default=str, ensure_ascii=False)
    except TypeError:
        return str(value)


def _load_artifact(
    run_dir: Path, case_id: str, variant_name: str
) -> FilesystemArtifact | None:
    """Read `runs/<id>/artifacts/<case>/<variant>/artifact.json` if present."""
    from eval_harness.core.models import FilesystemArtifact

    path = run_dir / "artifacts" / case_id / variant_name / "artifact.json"
    if not path.exists():
        return None
    return FilesystemArtifact.model_validate_json(path.read_text())


def _print_artifact(
    console: Console,
    artifact: FilesystemArtifact,
    *,
    truncate: bool,
) -> None:
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.table import Table

    diff = artifact.diff
    summary_table = Table(title="filesystem diff")
    summary_table.add_column("kind")
    summary_table.add_column("count", justify="right")
    summary_table.add_column("files")
    summary_table.add_row("added", str(len(diff.added)), _join_short(diff.added))
    summary_table.add_row("removed", str(len(diff.removed)), _join_short(diff.removed))
    summary_table.add_row("modified", str(len(diff.modified)), _join_short(diff.modified))
    console.print(summary_table)

    meta_lines = [
        f"workspace_kind: {artifact.workspace_kind}",
        f"artifacts_path: {artifact.artifacts_path}",
        f"before_files: {len(artifact.before_manifest.files)}",
        f"after_files: {len(artifact.after_manifest.files)}",
    ]
    console.print(Panel("\n".join(meta_lines), title="workspace", border_style="cyan"))

    # Per-file detail. For text diffs we have, show the unified-diff body
    # (truncated). For modified files without a text diff, show the after
    # manifest entry (size + sha) as a fallback.
    after_files = artifact.after_manifest.files
    for path in diff.modified + diff.added:
        if path in diff.text_diffs:
            body = diff.text_diffs[path]
            if truncate:
                body = _truncate_diff(body)
            console.print(
                Panel(
                    Syntax(body, "diff", word_wrap=True),
                    title=f"diff: {path}",
                    border_style="yellow",
                )
            )
        elif path in after_files:
            entry = after_files[path]
            console.print(
                Panel(
                    f"size: {entry.size} bytes\nsha256: {entry.sha256}",
                    title=f"binary/new: {path}",
                    border_style="yellow",
                )
            )


def _join_short(paths: list[str], limit: int = 6) -> str:
    if not paths:
        return "—"
    if len(paths) <= limit:
        return ", ".join(paths)
    head = ", ".join(paths[:limit])
    return f"{head}, … ({len(paths) - limit} more)"


def _truncate_diff(body: str) -> str:
    lines = body.splitlines()
    if len(lines) <= _DIFF_LINE_TRUNCATE:
        return body
    head = "\n".join(lines[:_DIFF_LINE_TRUNCATE])
    return (
        f"{head}\n… (truncated {len(lines) - _DIFF_LINE_TRUNCATE} more lines; "
        "pass --no-truncate)"
    )
