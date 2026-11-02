"""The model provider choice and the chosen provider's own questions."""

from openscientist.cli import livecheck
from openscientist.cli.step import Question, Step
from openscientist.providers import provider_class, provider_ids


class ProviderStep(Step):
    key = "provider"
    title = "Model provider"

    def questions(self, answers: dict[str, str]) -> list[Question]:
        provider_id = answers.get("OPENSCIENTIST_PROVIDER")
        if not provider_id or provider_id not in provider_ids():
            ids = provider_ids()
            return [
                Question.for_env(
                    key="provider.id",
                    env="OPENSCIENTIST_PROVIDER",
                    file="env",
                    kind="choice",
                    choices=ids,
                    labels=tuple(provider_class(pid).display_name for pid in ids),
                    help="which LLM backend serves the model",
                )
            ]
        provider_cls = provider_class(provider_id)
        return [
            Question.for_env(
                key=f"provider.{question.env.lower()}",
                env=question.env,
                file="env",
                kind=question.kind,
                help=question.help,
            )
            for question in provider_cls.setup_questions()
        ]

    def validate(self, values: dict[str, str]) -> list[str]:
        provider_id = values.get("OPENSCIENTIST_PROVIDER", "")
        if not provider_id:
            return ["OPENSCIENTIST_PROVIDER is required."]
        if provider_id not in provider_ids():
            return [f"Unknown provider {provider_id!r}. Valid options: {', '.join(provider_ids())}"]
        return provider_class(provider_id).setup_answers_errors(provider_id, values)

    def after_validate(self, answers: dict[str, str]) -> str | livecheck.LiveCheckOutcome:
        provider_id = answers.get("OPENSCIENTIST_PROVIDER", "")
        if not provider_id or provider_id not in provider_ids():
            return "pass"
        return livecheck.run_live_check(provider_id, answers)

    def declared_envs(self) -> frozenset[str]:
        envs = {"OPENSCIENTIST_PROVIDER"}
        for provider_id in provider_ids():
            envs.update(question.env for question in provider_class(provider_id).setup_questions())
        return frozenset(envs)
