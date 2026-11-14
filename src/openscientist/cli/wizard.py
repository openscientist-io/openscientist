"""The setup flow: the steps, the prompts, the answers file, the file writes."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path

import click
from rich.console import Console

from openscientist.cli import answers as answers_io
from openscientist.cli import envfiles
from openscientist.cli.livecheck import LiveCheckOutcome, live_check_result
from openscientist.cli.step import Question, Step
from openscientist.cli.steps import STEPS
from openscientist.cli.steps.provider import ProviderStep
from openscientist.providers import provider_class, provider_ids
from openscientist.providers.base import Alternatives

console = Console()

#: The answer that clears a saved value, where Enter would keep it.
CLEAR = "-"


def run_setup(root: Path, write_answers: Path | None = None) -> None:
    """Ask the steps, write the env files and generate the missing secrets."""
    answers, env_file, outcome = run_steps(root)
    _write_env_files(root, answers, env_file)
    if write_answers is not None:
        answers_io.write_answers(write_answers, run_tables(answers, outcome))


def run_setup_from_answers(
    root: Path, answers_file: Path, write_answers: Path | None = None
) -> int:
    """The wizard from an answers file: the same steps, validation and live check,
    without prompting.

    Returns 0 and writes the env files only when every answer is present and
    valid. Otherwise returns 1, naming every missing or invalid answer and
    writing nothing.
    """
    try:
        tables = answers_io.read_answers(answers_file)
    except answers_io.AnswersError as err:
        console.print(f"[red]{err}[/red]")
        return 1
    answers, env_file, errors = _run_steps_from_answers(root, tables)
    outcome: LiveCheckOutcome | None = None
    if not errors:
        provider_id = answers.get("OPENSCIENTIST_PROVIDER", "")
        if provider_id and provider_id in provider_ids():
            errors, outcome = _live_check_from_answers(tables.get("provider"), provider_id, answers)
    if errors:
        for error in errors:
            console.print(f"[red]{error}[/red]")
        return 1
    _write_env_files(root, answers, env_file)
    if write_answers is not None:
        answers_io.write_answers(write_answers, run_tables(answers, outcome))
    return 0


def _write_env_files(root: Path, answers: dict[str, str], env_file: dict[str, str]) -> None:
    """The answers into the env files the steps put them in, the missing secrets generated.

    An empty answer is written as ``KEY=`` only when it clears a saved value,
    so a commented example line stays commented.
    """
    saved = envfiles.current_values(root)
    by_file: dict[str, dict[str, str]] = {}
    for env, value in answers.items():
        if env in env_file and (value or saved.get(env)):
            by_file.setdefault(env_file[env], {})[env] = value
    # The cookie's Secure flag follows APP_URL, so the wizard writes it from the asked address.
    base_url = answers.get("OPENSCIENTIST_BASE_URL", "")
    base_file = env_file.get("OPENSCIENTIST_BASE_URL")
    if base_url and base_file and not envfiles.file_values(root, base_file).get("APP_URL"):
        by_file.setdefault(base_file, {})["APP_URL"] = base_url
    envfiles.write_env_files(root, by_file)
    envfiles.record_host_project_dir(root)


def run_steps(
    root: Path,
) -> tuple[dict[str, str], dict[str, str], LiveCheckOutcome | None]:
    """Drive the applicable steps, returning the answers, each answer's env file
    and the live check's outcome when the provider ran one."""
    answers = envfiles.current_values(root)
    saved = dict(answers)
    env_file: dict[str, str] = {}
    outcome: LiveCheckOutcome | None = None
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
            changed = {env for env, value in answers.items() if value and value != saved.get(env)}
            _settle_alternatives(step.alternatives(answers), answers, changed)
            errors = step.validate(answers)
            if errors:
                for error in errors:
                    console.print(f"[red]{error}[/red]")
                console.print("[yellow]Those answers do not validate, trying again.[/yellow]")
                continue
            verdict = step.after_validate(answers)
            if isinstance(verdict, LiveCheckOutcome):
                outcome = verdict
                verdict = verdict.verdict
            if verdict == "re-ask":
                continue
            break
    return answers, env_file, outcome


def _run_steps_from_answers(
    root: Path, tables: Mapping[str, Mapping[str, answers_io.Scalar]]
) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """The applicable steps against the file's tables: the answers, each answer's
    env file and the missing or invalid answers, each named."""
    answers = envfiles.current_values(root)
    env_file: dict[str, str] = {}
    errors: list[str] = []
    # The file states every answer, so each non-empty one counts as given, changed or not.
    supplied: set[str] = set()
    for step in STEPS:
        if not step.applies(answers):
            continue
        while True:
            batch = [q for q in step.questions(answers) if q.env not in env_file]
            if not batch:
                break
            for question in batch:
                env_file[question.env] = question.file
                key = answers_io.question_key(step.key, question.key)
                table = tables.get(step.key)
                if table is None or key not in table:
                    # A secret question is optional: the run keeps the current
                    # value, and a validation error names it when it is required.
                    if question.kind != "secret":
                        errors.append(f"Missing answer: {question.key}")
                    continue
                value = table[key]
                if question.kind in ("int", "port"):
                    if isinstance(value, bool):
                        errors.append(
                            f"Invalid answer: {question.key} must be a number, got {value!r}."
                        )
                        continue
                    answers[question.env] = str(value)
                elif isinstance(value, str):
                    answers[question.env] = value
                    if value:
                        supplied.add(question.env)
                else:
                    errors.append(
                        f"Invalid answer: {question.key} must be a string, got {type(value).__name__}."
                    )
        _settle_alternatives(step.alternatives(answers), answers, supplied)
        errors.extend(step.validate(answers))
    return answers, env_file, errors


def _settle_alternatives(
    groups: Alternatives, answers: dict[str, str], given: Collection[str]
) -> None:
    """Empty the other members of each group once exactly one member is among
    the ``given`` answers, so switching between alternatives keeps only the new one.

    Several members given at once are left for the validation to refuse.
    """
    for group in groups:
        given_members = [member for member in group if any(env in given for env in member)]
        if len(given_members) != 1:
            continue
        for member in group:
            if member != given_members[0]:
                for env in member:
                    answers[env] = ""


def _live_check_from_answers(
    table: Mapping[str, answers_io.Scalar] | None, provider_id: str, answers: dict[str, str]
) -> tuple[list[str], LiveCheckOutcome]:
    """The provider's live check without prompting: the errors when it cannot pass
    and the outcome for the answers the run wrote.

    A check that would make a billed request is skipped only when the file sets
    the provider's skip_live_check switch; every other check runs.
    """
    cls = provider_class(provider_id)
    skip_value = table.get(answers_io.SKIP_LIVE_CHECK) if table else None
    if cls.live_check_billed() and answers_io.is_true(skip_value):
        console.print("Skipped the billed live check.")
        return [], LiveCheckOutcome("pass", billed_check_skipped=True)
    console.print(f"Checking {cls.display_name}...")
    result = live_check_result(provider_id, answers)
    if not result.ok:
        errors = [f"The live check for {cls.display_name} failed: {result.detail}"]
        return errors, LiveCheckOutcome("pass")
    console.print(f"[green]{result.detail}[/green]")
    return [], LiveCheckOutcome("pass")


def run_tables(
    answers: Mapping[str, str], outcome: LiveCheckOutcome | None
) -> dict[str, dict[str, str | bool]]:
    """The run's answers as the answers file's tables, without the secret questions.

    A provider whose live check bills records whether the run skipped it, so a
    replay bills (or does not) the same way.
    """
    tables: dict[str, dict[str, str | bool]] = {}
    for step in STEPS:
        if not step.applies(dict(answers)):
            continue
        table: dict[str, str | bool] = {}
        for question in step_questions(step, answers):
            if question.kind == "secret":
                continue
            value = answers.get(question.env)
            if value is None:
                continue
            table[answers_io.question_key(step.key, question.key)] = value
        if isinstance(step, ProviderStep) and outcome is not None:
            provider_id = answers.get("OPENSCIENTIST_PROVIDER", "")
            if provider_id in provider_ids() and provider_class(provider_id).live_check_billed():
                table[answers_io.SKIP_LIVE_CHECK] = outcome.billed_check_skipped
        if table:
            tables[step.key] = table
    return tables


def step_questions(step: Step, answers: Mapping[str, str]) -> list[Question]:
    """Every question the step asks against ``answers``, in the order the run asked them."""
    seen: dict[str, Question] = {}
    for probe in ({}, dict(answers)):
        for question in step.questions(probe):
            seen.setdefault(question.env, question)
    return list(seen.values())


def ask(question: Question, current: str | None) -> str:
    """One prompt for ``question``; the current value is the default, then the settings default."""
    if question.kind == "choice":
        return _ask_choice(question, current)
    text = question.env
    if question.help:
        console.print(f"[dim]{question.help}[/dim]")
    default = current or question.default
    if question.kind == "secret":
        console.print(
            "[dim]Input is hidden as you type or paste, so press Enter when finished.[/dim]"
        )
        # Never show a secret as the prompt default, and let Enter mean "keep
        # the current" or "leave empty", which click's prompt would re-ask for.
        if current:
            text += f" (Enter keeps the current value, {CLEAR} clears it)"
        value = click.prompt(text, default=default or "", hide_input=True, show_default=False)
        if value == CLEAR:
            return ""
        return value if value else (current or "")
    if question.kind in ("int", "port"):
        if default is None:
            return click.prompt(text) or ""
        value = click.prompt(text, default=default, type=int)
        return "" if value is None else str(value)
    if current:
        console.print(f"[dim]Enter keeps it, {CLEAR} clears it.[/dim]")
    while True:
        value = click.prompt(text, default=default or "", show_default=default is not None)
        if value == CLEAR:
            value = ""
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
