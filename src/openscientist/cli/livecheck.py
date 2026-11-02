"""The wizard's live provider check: the billed warning, the run, the failure choices."""

from __future__ import annotations

import socket
from collections.abc import Callable, Mapping
from urllib.parse import urlsplit, urlunsplit

import click
from rich.console import Console

from openscientist.providers import provider_class
from openscientist.providers.base import LiveCheckResult

console = Console()

# Containers reach the host by this name, which a Linux host itself does not resolve.
_CONTAINER_HOST = "host.docker.internal"


def _resolves(name: str) -> bool:
    try:
        socket.getaddrinfo(name, None)
    except OSError:
        return False
    return True


def host_view(
    answers: Mapping[str, str], resolves: Callable[[str], bool] = _resolves
) -> dict[str, str]:
    """The answers as the host reaches them, with the containers' host name as loopback when the host cannot resolve it."""
    if resolves(_CONTAINER_HOST):
        return dict(answers)
    seen: dict[str, str] = {}
    for key, value in answers.items():
        parts = urlsplit(value)
        if parts.hostname == _CONTAINER_HOST:
            netloc = parts.netloc.replace(_CONTAINER_HOST, "127.0.0.1", 1)
            value = urlunsplit(parts._replace(netloc=netloc))
        seen[key] = value
    return seen


def perform_live_check(provider_id: str, answers: dict[str, str]) -> LiveCheckResult:
    """The boundary the wizard crosses to reach the provider. Tests fake this."""
    return provider_class(provider_id).live_check(provider_id, dict(answers))


def run_live_check(provider_id: str, answers: dict[str, str]) -> str:
    """Run the live check after the provider's own validation passes.

    Returns 'pass' to proceed, or 're-ask' to re-ask the provider questions.
    """
    cls = provider_class(provider_id)
    if cls.live_check_billed():
        console.print(
            "[yellow]This live check makes a billed request (a one-token completion).[/yellow]"
        )
        if not _confirm("Run the live check? (y/N) "):
            console.print("Skipped the live check.")
            return "pass"
    console.print(f"Checking {cls.display_name}...")
    probe_answers = host_view(answers, _resolves)
    result = perform_live_check(provider_id, probe_answers)
    while not result.ok:
        console.print("[red]The live check failed.[/red]")
        console.print(result.detail)
        choice = _failure_choice()
        if choice == "f":
            return "re-ask"
        if choice == "s":
            console.print("Saving the settings without a passing live check.")
            return "pass"
        console.print(f"Retrying {cls.display_name}...")
        result = perform_live_check(provider_id, probe_answers)
    console.print(f"[green]{result.detail}[/green]")
    return "pass"


def _confirm(prompt: str) -> bool:
    value = click.prompt(prompt, default="n", show_default=False).strip().lower()
    return value in ("y", "yes")


def _failure_choice() -> str:
    """One character: f to fix, r to retry, s to save anyway."""
    value = (
        click.prompt(
            "Fix the provider settings (f), retry (r) or save anyway (s)?",
            default="f",
            show_default=False,
        )
        .strip()
        .lower()
    )
    return value[:1] if value else "f"
