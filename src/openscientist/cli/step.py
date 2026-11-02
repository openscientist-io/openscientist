"""The setup wizard's typed questions and steps."""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import ClassVar

from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from pydantic_settings import BaseSettings

from openscientist.settings import (
    AgentSettings,
    AirgapSettings,
    AuthSettings,
    BerkeleyLabSettings,
    BudgetSettings,
    ContainerSettings,
    DatabaseSettings,
    DevSettings,
    FileSettings,
    MigrationSettings,
    PhenixSettings,
    ProviderSettings,
    Settings,
)

#: Every settings class the wizard takes defaults and help from.
SETTINGS_CLASSES: tuple[type[BaseSettings], ...] = (
    Settings,
    DevSettings,
    ProviderSettings,
    DatabaseSettings,
    MigrationSettings,
    AuthSettings,
    BudgetSettings,
    FileSettings,
    ContainerSettings,
    PhenixSettings,
    BerkeleyLabSettings,
    AgentSettings,
    AirgapSettings,
)


def settings_field_info(env: str) -> FieldInfo | None:
    """The settings field whose alias is ``env``, if one exists."""
    for settings_cls in SETTINGS_CLASSES:
        for info in settings_cls.model_fields.values():
            if info.alias == env:
                return info
    return None


@dataclass(frozen=True)
class Question:
    """One wizard question and the env variable its answer lands in.

    ``kind`` is choice, text, secret, flag, path, port or int. ``file`` is the
    env file the answer goes to: env, db or owner.
    """

    key: str
    env: str
    file: str
    kind: str
    choices: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    default: str | None = None
    help: str | None = None
    required: bool = False

    @classmethod
    def for_env(
        cls,
        key: str,
        env: str,
        file: str,
        kind: str,
        *,
        choices: tuple[str, ...] = (),
        labels: tuple[str, ...] = (),
        default: str | None = None,
        help: str | None = None,
        required: bool = False,
    ) -> Question:
        """A question for ``env``, taking help and default from the settings field when not given."""
        info = settings_field_info(env)
        if info is not None:
            if help is None:
                help = info.description
            if default is None:
                settings_default = info.default
                if (
                    settings_default is not None
                    and settings_default is not PydanticUndefined
                    and settings_default != ""
                ):
                    default = str(settings_default)
        return cls(
            key=key,
            env=env,
            file=file,
            kind=kind,
            choices=choices,
            labels=labels,
            default=default,
            help=help,
            required=required,
        )


class Step(abc.ABC):
    """One wizard section: when it applies, what it asks, how it checks the answers."""

    key: ClassVar[str]
    title: ClassVar[str]

    def applies(self, answers: dict[str, str]) -> bool:
        return True

    @abc.abstractmethod
    def questions(self, answers: dict[str, str]) -> list[Question]:
        """The questions to ask now, given the answers so far."""

    def validate(self, values: dict[str, str]) -> list[str]:
        """Errors in this step's answers; empty when they are accepted."""
        return []

    def after_validate(self, answers: dict[str, str]) -> str:
        """Run after ``validate`` passes. 'pass' to continue, 're-ask' to re-ask
        this step's questions (the live provider check)."""
        return "pass"

    def declared_envs(self) -> frozenset[str]:
        """Every env variable this step can ask for, for the drift test."""
        return frozenset()
