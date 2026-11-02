"""The optional Phenix install: the host path the containers mount at /opt/phenix."""

from __future__ import annotations

from openscientist.cli.step import Question, Step
from openscientist.settings import phenix_installation_ok


class PhenixStep(Step):
    key = "phenix"
    title = "Phenix"

    def questions(self, answers: dict[str, str]) -> list[Question]:
        return [
            Question.for_env(
                key="phenix.host_path",
                env="PHENIX_HOST_PATH",
                file="env",
                kind="path",
                help="optional. host path to a Phenix 2.x install; mounted read-only at /opt/phenix",
            )
        ]

    def validate(self, values: dict[str, str]) -> list[str]:
        host_path = values.get("PHENIX_HOST_PATH", "")
        if not host_path:
            return []
        if not host_path.startswith("/"):
            return [
                f"PHENIX_HOST_PATH must be an absolute path (starting with '/'), "
                f"got: {host_path!r}."
            ]
        if ".." in host_path:
            return [f"PHENIX_HOST_PATH must not contain path traversal (..): {host_path!r}."]
        if not phenix_installation_ok(host_path):
            return [
                f"PHENIX_HOST_PATH={host_path!r} is not a Phenix 2.x install "
                "(missing bin/phenix.about)."
            ]
        return []

    def declared_envs(self) -> frozenset[str]:
        return frozenset({"PHENIX_HOST_PATH"})
