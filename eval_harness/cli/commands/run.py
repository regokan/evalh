from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import click


@click.command("run")
@click.argument(
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
def run(config_path: Path) -> None:
    """Run an eval defined by eval.yaml."""
    # Heavy imports happen here so `evalh --help` stays fast.
    from eval_harness.core.config_loader import load_config
    from eval_harness.core.errors import ConfigError

    try:
        config = load_config(config_path)
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        summary = asyncio.run(_main(config, config_path))
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        click.echo(f"error: {type(exc).__name__}: {exc}", err=True)
        sys.exit(2)

    _print_summary(summary)


async def _main(config: object, config_path: Path) -> object:
    from eval_harness.runner import build_plan, run_eval

    plan = await build_plan(config, config_path)  # type: ignore[arg-type]
    return await run_eval(plan)


def _print_summary(summary: object) -> None:
    from rich.console import Console
    from rich.table import Table

    from eval_harness.core.models import RunSummary

    if not isinstance(summary, RunSummary):
        click.echo(str(summary))
        return

    console = Console()
    console.print(f"[bold]run_id[/]: {summary.run_id}")
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

    if summary.comparison is not None and summary.comparison.deltas:
        cmp_table = Table(title=f"Comparison vs baseline '{summary.comparison.baseline}'")
        cmp_table.add_column("variant")
        cmp_table.add_column("pass_rate_delta", justify="right")
        cmp_table.add_column("regressions", justify="right")
        cmp_table.add_column("improvements", justify="right")
        for d in summary.comparison.deltas:
            cmp_table.add_row(
                d.variant,
                f"{d.pass_rate_delta:+.2%}",
                str(len(d.regressions)),
                str(len(d.improvements)),
            )
        console.print(cmp_table)
