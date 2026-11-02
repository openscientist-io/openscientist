"""The job concurrency."""

from openscientist.cli.step import Question, Step


class ConcurrencyStep(Step):
    key = "concurrency"
    title = "Job concurrency"

    def questions(self, answers: dict[str, str]) -> list[Question]:
        return [
            Question.for_env(
                key="concurrency.jobs",
                env="OPENSCIENTIST_MAX_CONCURRENT_JOBS",
                file="env",
                kind="int",
                help="jobs running at the same time",
            )
        ]

    def validate(self, values: dict[str, str]) -> list[str]:
        raw = values.get("OPENSCIENTIST_MAX_CONCURRENT_JOBS", "")
        if not raw:
            return []
        try:
            jobs = int(raw)
        except ValueError:
            return [f"OPENSCIENTIST_MAX_CONCURRENT_JOBS must be a number, got {raw!r}."]
        if jobs < 1:
            return [f"OPENSCIENTIST_MAX_CONCURRENT_JOBS must be at least 1, got {jobs}."]
        return []
