"""The agent harness choice."""

from openscientist.cli.step import Question, Step

HARNESSES = ("auto", "claude_code", "codex", "omp")


class HarnessStep(Step):
    key = "harness"
    title = "Agent harness"

    def questions(self, answers: dict[str, str]) -> list[Question]:
        return [
            Question.for_env(
                key="harness.id",
                env="OPENSCIENTIST_HARNESS",
                file="env",
                kind="choice",
                choices=HARNESSES,
                help="which coding agent drives the jobs; auto derives it from the provider",
            )
        ]
