"""The air-gapped mode switch, offered only for providers that can be air-gapped."""

from __future__ import annotations

import logging
from collections.abc import Mapping

from pydantic import ValidationError

from openscientist.cli.step import Question, Step
from openscientist.providers import provider_class, provider_ids
from openscientist.providers.base import (
    AirgapEgress,
    AirgapPosture,
    ProviderSettings,
    _answers_env,
)


def provider_airgap_posture(provider_id: str, answers: Mapping[str, str]) -> AirgapPosture:
    """The provider's air-gapped egress posture, evaluated against the wizard's answers.

    The posture reads the provider's own settings, so the answers are held in the
    process environment for the duration of the probe. The probe builds only the
    provider settings (not the full app), so it runs before the database and the
    master secret exist.
    """
    values = dict(answers)
    values["OPENSCIENTIST_PROVIDER"] = provider_id
    settings_logger = logging.getLogger("openscientist.settings")
    previous_level = settings_logger.level
    settings_logger.setLevel(logging.ERROR)
    try:
        with _answers_env(values):
            try:
                provider = ProviderSettings(_env_file=None)
            except ValidationError:
                return AirgapPosture(
                    AirgapEgress.UNSUPPORTED, reason="Provider configuration is incomplete."
                )
            return provider_class(provider_id).airgap_posture(provider)
    finally:
        settings_logger.setLevel(previous_level)


class AirgapStep(Step):
    key = "airgap"
    title = "Air-gapped mode"

    def applies(self, answers: dict[str, str]) -> bool:
        provider_id = answers.get("OPENSCIENTIST_PROVIDER", "")
        if not provider_id or provider_id not in provider_ids():
            return False
        return provider_airgap_posture(provider_id, answers).mode is not AirgapEgress.UNSUPPORTED

    def questions(self, answers: dict[str, str]) -> list[Question]:
        return [
            Question.for_env(
                key="airgap.enabled",
                env="OPENSCIENTIST_AIRGAPPED",
                file="env",
                kind="text",
            )
        ]

    def declared_envs(self) -> frozenset[str]:
        return frozenset({"OPENSCIENTIST_AIRGAPPED"})
