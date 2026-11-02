"""The public address and the published ports."""

from openscientist.cli.step import Question, Step

_PORTS = ("PORT", "POSTGRES_PORT")


class AddressStep(Step):
    key = "address"
    title = "Address and ports"

    def questions(self, answers: dict[str, str]) -> list[Question]:
        return [
            Question.for_env(
                key="address.base_url",
                env="OPENSCIENTIST_BASE_URL",
                file="env",
                kind="text",
                help="public base URL of the web, for links and notifications",
            ),
            # Neither port is a settings field, so the defaults follow .env.example.
            Question.for_env(
                key="address.port", env="PORT", file="env", kind="port", default="8080"
            ),
            Question.for_env(
                key="address.postgres_port",
                env="POSTGRES_PORT",
                file="env",
                kind="port",
                default="5434",
            ),
        ]

    def validate(self, values: dict[str, str]) -> list[str]:
        errors: list[str] = []
        base_url = values.get("OPENSCIENTIST_BASE_URL", "")
        if base_url and not base_url.startswith(("http://", "https://")):
            errors.append("OPENSCIENTIST_BASE_URL must be an http(s) URL.")
        for key in _PORTS:
            errors.extend(self._valid_port(key, values.get(key, "")))
        return errors

    @staticmethod
    def _valid_port(key: str, raw: str) -> list[str]:
        if not raw:
            return []
        try:
            port = int(raw)
        except ValueError:
            return [f"{key} must be a number, got {raw!r}."]
        if not 1 <= port <= 65535:
            return [f"{key} must be a port between 1 and 65535, got {port}."]
        return []
