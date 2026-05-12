from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from rich.console import Console


@dataclass
class _RunSnapshot:
    label: str
    # case_id -> set of variant names that passed under config.pass_criteria.
    pass_by_case: dict[str, set[str]] = field(default_factory=dict)
    fail_by_case: dict[str, set[str]] = field(default_factory=dict)
    variant_pass_rates: dict[str, float] = field(default_factory=dict)
    evaluator_pass_rates: dict[str, float] = field(default_factory=dict)


@click.command("compare")
@click.argument(
    "run_a",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.argument(
    "run_b",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
def compare(run_a: Path, run_b: Path) -> None:
    """Diff two runs by case_id and report regressions / improvements.

    Informational only — always exits 0. CI gating lands in v0.2.
    """
    from eval_harness.core.errors import ConfigError

    try:
        snap_a, snap_b = asyncio.run(_load_both(run_a, run_b))
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    _print_comparison(snap_a, snap_b)


async def _load_both(run_a: Path, run_b: Path) -> tuple[_RunSnapshot, _RunSnapshot]:
    snap_a, snap_b = await asyncio.gather(
        _load_run(run_a, "A"),
        _load_run(run_b, "B"),
    )
    return snap_a, snap_b


async def _load_run(run_dir: Path, label: str) -> _RunSnapshot:
    from eval_harness.core.run_reader import RunReader

    reader = RunReader(run_dir)
    summary = await reader.load_summary()

    variant_pass_rates = {v.name: v.pass_rate for v in summary.variants}
    evaluator_pass_rates: dict[str, float] = {}
    for rollup in summary.by_evaluator:
        rates = [v.pass_rate for v in rollup.by_variant.values()]
        evaluator_pass_rates[rollup.evaluator] = (
            sum(rates) / len(rates) if rates else 0.0
        )

    # Compute per-(case_id, variant) pass/fail by AND-ing every evaluator result
    # for that cell. This mirrors "case passes when every evaluator passed"
    # which matches the v0 default pass_criteria. If the run used a richer
    # pass_criteria it's still a reasonable cross-run diff — the bead asks us
    # to diff cases, not re-evaluate criteria.
    pass_map: dict[tuple[str, str], bool] = defaultdict(lambda: True)
    seen: set[tuple[str, str]] = set()
    async for result in reader.iter_results():
        key = (result.case_id, result.variant_name)
        seen.add(key)
        if not (result.passed and result.error is None):
            pass_map[key] = False
        elif key not in pass_map:
            pass_map[key] = True

    pass_by_case: dict[str, set[str]] = defaultdict(set)
    fail_by_case: dict[str, set[str]] = defaultdict(set)
    for (case_id, variant_name) in seen:
        if pass_map[(case_id, variant_name)]:
            pass_by_case[case_id].add(variant_name)
        else:
            fail_by_case[case_id].add(variant_name)

    return _RunSnapshot(
        label=label,
        pass_by_case=dict(pass_by_case),
        fail_by_case=dict(fail_by_case),
        variant_pass_rates=variant_pass_rates,
        evaluator_pass_rates=evaluator_pass_rates,
    )


def _print_comparison(a: _RunSnapshot, b: _RunSnapshot) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()

    regressions, improvements = _case_deltas(a, b)
    only_in_a = sorted(set(a.pass_by_case) | set(a.fail_by_case))
    only_in_b = sorted(set(b.pass_by_case) | set(b.fail_by_case))
    only_in_a_set = set(only_in_a) - set(only_in_b)
    only_in_b_set = set(only_in_b) - set(only_in_a)

    _print_case_diff_table(console, "Regressions (passed in A, failed in B)", regressions)
    _print_case_diff_table(console, "Improvements (failed in A, passed in B)", improvements)
    _print_pass_rate_table(
        console,
        title="Per-variant pass-rate delta",
        a_map=a.variant_pass_rates,
        b_map=b.variant_pass_rates,
        row_label="variant",
    )
    _print_pass_rate_table(
        console,
        title="Per-evaluator pass-rate delta",
        a_map=a.evaluator_pass_rates,
        b_map=b.evaluator_pass_rates,
        row_label="evaluator",
    )

    if only_in_a_set or only_in_b_set:
        table = Table(title="Cases present in only one run")
        table.add_column("case_id")
        table.add_column("present_in")
        for case_id in sorted(only_in_a_set):
            table.add_row(case_id, "A only")
        for case_id in sorted(only_in_b_set):
            table.add_row(case_id, "B only")
        console.print(table)


def _case_deltas(
    a: _RunSnapshot, b: _RunSnapshot
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    regressions: list[tuple[str, str]] = []
    improvements: list[tuple[str, str]] = []
    shared = (set(a.pass_by_case) | set(a.fail_by_case)) & (
        set(b.pass_by_case) | set(b.fail_by_case)
    )
    for case_id in sorted(shared):
        a_pass = a.pass_by_case.get(case_id, set())
        a_fail = a.fail_by_case.get(case_id, set())
        b_pass = b.pass_by_case.get(case_id, set())
        b_fail = b.fail_by_case.get(case_id, set())
        shared_variants = (a_pass | a_fail) & (b_pass | b_fail)
        for variant in sorted(shared_variants):
            in_a_pass = variant in a_pass
            in_b_pass = variant in b_pass
            if in_a_pass and not in_b_pass:
                regressions.append((case_id, variant))
            elif not in_a_pass and in_b_pass:
                improvements.append((case_id, variant))
    return regressions, improvements


def _print_case_diff_table(
    console: Console, title: str, rows: list[tuple[str, str]]
) -> None:
    from rich.table import Table

    table = Table(title=f"{title} ({len(rows)})")
    table.add_column("case_id")
    table.add_column("variant")
    for case_id, variant in rows:
        table.add_row(case_id, variant)
    console.print(table)


def _print_pass_rate_table(
    console: Console,
    *,
    title: str,
    a_map: dict[str, float],
    b_map: dict[str, float],
    row_label: str,
) -> None:
    from rich.table import Table

    table = Table(title=title)
    table.add_column(row_label)
    table.add_column("A", justify="right")
    table.add_column("B", justify="right")
    table.add_column("Δ", justify="right")
    for name in sorted(set(a_map) | set(b_map)):
        a_val = a_map.get(name)
        b_val = b_map.get(name)
        delta = (b_val - a_val) if (a_val is not None and b_val is not None) else None
        table.add_row(
            name,
            "—" if a_val is None else f"{a_val:.2%}",
            "—" if b_val is None else f"{b_val:.2%}",
            "—" if delta is None else f"{delta:+.2%}",
        )
    console.print(table)


