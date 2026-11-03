"""The openscientist CLI: set up, start and stop the stack."""

from __future__ import annotations

from pathlib import Path

import click
import typer
from rich.console import Console

from openscientist.cli import envfiles, stack, wizard

app = typer.Typer(help="Set up and run OpenScientist.", no_args_is_help=False, add_completion=False)
console = Console()


@app.command()
def setup(
    answers: Path | None = typer.Option(
        None,
        "--answers",
        help="TOML answers file: one table per step, then the step's question keys.",
    ),
    write_answers: Path | None = typer.Option(
        None,
        "--write-answers",
        help="Write the run's answers to this TOML file, never the secrets.",
    ),
) -> None:
    """Run the setup wizard, keeping the generated secrets."""
    if answers is not None:
        exit_code = wizard.run_setup_from_answers(Path.cwd(), answers, write_answers)
        if exit_code:
            raise typer.Exit(exit_code)
        console.print("Wrote .env, .env.db and .env.owner. Run `uv run openscientist` to start.")
        return
    wizard.run_setup(Path.cwd(), write_answers)
    console.print("Wrote .env, .env.db and .env.owner. Run `uv run openscientist` to start.")


@app.command()
def stop() -> None:
    """Stop the stack, keeping the data volume."""
    exit_code = stack.stop(Path.cwd(), stack.run_command)
    if exit_code:
        raise typer.Exit(exit_code)


@app.callback(invoke_without_command=True)
def default_command(ctx: click.Context) -> None:
    """Set up when the checkout is not configured, then start the stack."""
    if ctx.invoked_subcommand is not None:
        return
    root = Path.cwd()
    if envfiles.is_configured(root):
        _start(root)
    else:
        console.print("Not configured yet, running the setup wizard.")
        wizard.run_setup(root)
        _start(root)


def _start(root: Path) -> None:
    exit_code = stack.start(root, stack.run_command)
    if exit_code:
        raise typer.Exit(exit_code)


def main() -> None:
    """Console script entry point."""
    app()
