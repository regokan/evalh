from __future__ import annotations

import click

from eval_harness.cli.commands.run import run as run_command


@click.group()
@click.version_option(package_name="eval-harness", prog_name="evalh")
def cli() -> None:
    """evalh — config-driven harness for evaluating AI systems."""


cli.add_command(run_command)


if __name__ == "__main__":
    cli()
