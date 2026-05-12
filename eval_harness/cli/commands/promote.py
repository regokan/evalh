"""`evalh promote` — mark a run as the baseline for its eval_name.

Reads `eval.name` from the run's `config.yaml` (or accepts an explicit
override via `--eval-name`), then atomically creates / replaces the
symlink at `<runs_root>/baselines/<eval_name>/` to point at the run dir.

`--as-baseline` is the default (and currently only) mode; the flag is
explicit so that forward-compat additions (e.g. promoting a run as the
"production model" version) don't shift the meaning of bare `evalh
promote`.
"""

from __future__ import annotations

from pathlib import Path

import click

from eval_harness.core.baseline import promote_run_to_baseline
from eval_harness.core.errors import ConfigError


@click.command("promote")
@click.argument(
    "run_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--as-baseline",
    "as_baseline",
    is_flag=True,
    default=True,
    help="Promote this run as the baseline for its eval_name (default).",
)
@click.option(
    "--eval-name",
    "eval_name",
    default=None,
    help="Override the eval_name read from config.yaml.",
)
@click.option(
    "--runs-root",
    "runs_root",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Where the baselines/ marker lives. Defaults to the run's parent.",
)
def promote(
    run_dir: Path,
    as_baseline: bool,
    eval_name: str | None,
    runs_root: Path | None,
) -> None:
    """Promote a finished run to be the baseline for its eval.

    The marker is a symlink at ``<runs_root>/baselines/<eval_name>/`` —
    ``ls runs/baselines/`` shows what's promoted at a glance.
    """
    if not as_baseline:
        # Forward-compat: `--no-as-baseline` would let future flag combos
        # express "promote as production model" etc. Today, there's only
        # one shape, so reject the no-op for clarity.
        raise click.ClickException(
            "evalh promote currently only supports --as-baseline (the default)."
        )

    try:
        link = promote_run_to_baseline(
            run_dir, eval_name=eval_name, runs_root=runs_root
        )
    except ConfigError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"promoted {run_dir} -> {link}")
