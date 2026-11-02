"""The interactive setup flow: the steps, the prompts, the file writes."""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from openscientist.cli import envfiles
from openscientist.cli.step import Question
from openscientist.cli.steps import STEPS

console = Console()


def run_setup(root: Path) -> None:
    """Ask the steps, write the env files and generate the missing secrets."""
    answers, env_file = run_steps(root)
    by_file: dict[str, dict[str, str]] = {}
    for env, value in answers.items():
        if value and env in env_file:
            by_file.setdefault(env_file[env], {})[env] = value
    envfiles.write_env_files(root, by_file)


def run_steps(root: Path) -> tuple[dict[str, str], dict[str, str]]:
    """Drive the applicable steps, returning the answers and each answer's env file."""
    answers = envfiles.current_values(root)
    env_file: dict[str, str] = {}
    for step in STEPS:
        if not step.applies(answers):
            continue
        console.rule(step.title)
        while True:
            answered_here: set[str] = set()
            # A step's questions can depend on its own answers (the provider
            # choice unlocks the provider's questions), so ask to a fixed point.
            while True:
                batch = [q for q in step.questions(answers) if q.env not in answered_here]
                if not batch:
                    break
                for question in batch:
                    answers[question.env] = ask(question, answers.get(question.env))
                    answered_here.add(question.env)
                    env_file[question.env] = question.file
            errors = step.validate(answers)
            if errors:
                for error in errors:
                    console.print(f"[red]{error}[/red]")
                console.print("[yellow]Those answers do not validate, trying again.[/yellow]")
                continue
            if step.after_validate(answers) == "re-ask":
                continue
            break
    return answers, env_file


def ask(question: Question, current: str | None) -> str:
    """One prompt for ``question``; the current value is the default, then the settings default."""
    if question.kind == "choice":
        return _ask_choice(question, current)
    text = question.env
    if question.help:
        console.print(f"[dim]{question.help}[/dim]")
    default = current or question.default
    if question.kind == "secret":
        # Never show a secret as the prompt default, and let Enter mean "keep
        # the current" or "leave empty", which click's prompt would re-ask for.
        if current:
            text += " (Enter keeps the current value)"
        value = click.prompt(text, default=default or "", hide_input=True, show_default=False)
        return value if value else (current or "")
    if question.kind in ("int", "port"):
        if default is None:
            return click.prompt(text) or ""
        value = click.prompt(text, default=default, type=int)
        return "" if value is None else str(value)
    while True:
        value = click.prompt(text, default=default or "", show_default=default is not None)
        if value or not question.required:
            break
        console.print("[red]This one is required.[/red]")
    return value or ""


def _ask_choice(question: Question, current: str | None) -> str:
    if question.help:
        console.print(f"[dim]{question.help}[/dim]")
    labels = question.labels or question.choices
    for index, label in enumerate(labels, 1):
        console.print(f"{index}. {label}")
    default_index = 1
    if current and current in question.choices:
        default_index = question.choices.index(current) + 1
    elif question.default and question.default in question.choices:
        default_index = question.choices.index(question.default) + 1
    index = click.prompt(
        question.env, default=default_index, type=click.IntRange(1, len(question.choices))
    )
    return question.choices[int(index) - 1]
