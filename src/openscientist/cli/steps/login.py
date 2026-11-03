"""The login method: the OAuth client pairs, the dev mock login, and the bootstrap admins."""

from __future__ import annotations

from openscientist.cli.step import Question, Step
from openscientist.settings import is_local_http_url

_OAUTH_PROVIDERS = ("google", "github", "orcid")


class LoginStep(Step):
    key = "login"
    title = "Login"

    def questions(self, answers: dict[str, str]) -> list[Question]:
        base_url = (answers.get("OPENSCIENTIST_BASE_URL") or "http://localhost:8080").rstrip("/")
        questions: list[Question] = []
        for provider in _OAUTH_PROVIDERS:
            client_id_env = f"{provider.upper()}_CLIENT_ID"
            client_secret_env = f"{provider.upper()}_CLIENT_SECRET"
            callback = f"{base_url}/auth/{provider}/callback"
            questions.append(
                Question.for_env(
                    key=f"login.{provider}_client_id",
                    env=client_id_env,
                    file="env",
                    kind="text",
                    help=f"optional. register the callback {callback} with the provider",
                )
            )
            questions.append(
                Question.for_env(
                    key=f"login.{provider}_client_secret",
                    env=client_secret_env,
                    file="env",
                    kind="secret",
                )
            )
        # A current dev-mode value stays asked, so a public base URL can turn it off.
        if is_local_http_url(base_url) or self._dev_mode_on(answers):
            questions.append(
                Question.for_env(
                    key="login.dev_mode",
                    env="OPENSCIENTIST_DEV_MODE",
                    file="env",
                    kind="text",
                    help="set true for the development mock login. Not for a public deployment.",
                )
            )
        questions.append(
            Question.for_env(
                key="login.bootstrap_admin_emails",
                env="BOOTSTRAP_ADMIN_EMAILS",
                file="env",
                kind="text",
            )
        )
        return questions

    def validate(self, values: dict[str, str]) -> list[str]:
        errors: list[str] = []
        for provider in _OAUTH_PROVIDERS:
            client_id = values.get(f"{provider.upper()}_CLIENT_ID", "")
            client_secret = values.get(f"{provider.upper()}_CLIENT_SECRET", "")
            if bool(client_id) != bool(client_secret):
                errors.append(
                    f"{provider.upper()}_CLIENT_ID and {provider.upper()}_CLIENT_SECRET "
                    "must both be set or both left empty."
                )
        # An empty base URL is the wizard's default, which is local.
        base_url = values.get("OPENSCIENTIST_BASE_URL") or "http://localhost:8080"
        local = is_local_http_url(base_url)
        if self._dev_mode_on(values) and not local:
            errors.append(
                "OPENSCIENTIST_DEV_MODE=true needs a local base URL (http with host "
                f"localhost, 127.0.0.1 or ::1), the base URL is {base_url!r}."
            )
        if not self._has_login_method(values):
            if local:
                errors.append(
                    "Configure at least one login method: an OAuth client pair "
                    "(Google, GitHub or ORCID) or the development mock login "
                    "(OPENSCIENTIST_DEV_MODE=true)."
                )
            else:
                errors.append(
                    "Configure at least one login method: an OAuth client pair "
                    "(Google, GitHub or ORCID)."
                )
        return errors

    @staticmethod
    def _has_login_method(values: dict[str, str]) -> bool:
        if any(values.get(f"{provider.upper()}_CLIENT_ID", "") for provider in _OAUTH_PROVIDERS):
            return True
        return LoginStep._dev_mode_on(values)

    @staticmethod
    def _dev_mode_on(values: dict[str, str]) -> bool:
        return values.get("OPENSCIENTIST_DEV_MODE", "").strip().lower() in (
            "true",
            "1",
            "yes",
            "y",
            "on",
        )

    def declared_envs(self) -> frozenset[str]:
        envs = set()
        for provider in _OAUTH_PROVIDERS:
            envs.add(f"{provider.upper()}_CLIENT_ID")
            envs.add(f"{provider.upper()}_CLIENT_SECRET")
        envs.add("OPENSCIENTIST_DEV_MODE")
        envs.add("BOOTSTRAP_ADMIN_EMAILS")
        return frozenset(envs)
