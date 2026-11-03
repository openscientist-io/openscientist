"""Launch-time refusal for provider traffic the LLM proxy cannot route.

A misconfiguration (no credential the proxy can sign with, or a harness that
cannot point the provider at the proxy) MUST refuse at launch with a clear
error rather than 502 a turn mid-run. The checks live on the provider
(``prelaunch_routing_error``) so they run where the container is started.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openscientist.providers import provider_class
from openscientist.settings import clear_settings_cache
from tests.omp_vault import make_jwt, make_vault


@pytest.fixture(autouse=True)
def _settings_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENSCIENTIST_SECRET_KEY", "0123456789abcdef" * 4)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@h:5432/db")
    clear_settings_cache()
    yield
    clear_settings_cache()


def _fresh_ms() -> int:
    return int((datetime.now(UTC) + timedelta(hours=1)).timestamp() * 1000)


class TestPrelaunchRoutingError:
    """The provider's prelaunch routing check for the vault-driven setup."""

    def test_openai_chatgpt_login_refused_under_the_omp_harness(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = make_vault(
            tmp_path,
            {
                "openai-codex": {
                    "access": make_jwt(datetime.now(UTC) + timedelta(hours=1)),
                    "refresh": "rt",
                    "expires": _fresh_ms(),
                }
            },
        )
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "openai")
        monkeypatch.setenv("OPENSCIENTIST_HARNESS", "omp")
        monkeypatch.setenv("OMP_AUTH_HOST_PATH", str(vault.parent))
        clear_settings_cache()
        provider = provider_class("openai")()
        error = provider.prelaunch_routing_error()
        assert error is not None
        assert "codex" in error.lower()

    def test_openai_chatgpt_login_allowed_under_the_codex_harness(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = make_vault(
            tmp_path,
            {
                "openai-codex": {
                    "access": make_jwt(datetime.now(UTC) + timedelta(hours=1)),
                    "refresh": "rt",
                    "expires": _fresh_ms(),
                }
            },
        )
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "openai")
        monkeypatch.setenv("OPENSCIENTIST_HARNESS", "codex")
        monkeypatch.setenv("OMP_AUTH_HOST_PATH", str(vault.parent))
        clear_settings_cache()
        provider = provider_class("openai")()
        assert provider.prelaunch_routing_error() is None

    def test_openai_api_key_under_omp_is_not_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "openai")
        monkeypatch.setenv("OPENSCIENTIST_HARNESS", "omp")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
        clear_settings_cache()
        provider = provider_class("openai")()
        assert provider.prelaunch_routing_error() is None

    def test_anthropic_vault_login_under_omp_is_not_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        vault = make_vault(
            tmp_path,
            {
                "anthropic": {
                    "access": "vault-anthropic-access",
                    "refresh": "rt",
                    "expires": _fresh_ms(),
                    "orgId": "org-1",
                }
            },
        )
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
        monkeypatch.setenv("OPENSCIENTIST_HARNESS", "omp")
        monkeypatch.setenv("OMP_AUTH_HOST_PATH", str(vault.parent))
        clear_settings_cache()
        provider = provider_class("anthropic")()
        assert provider.prelaunch_routing_error() is None

    def test_anthropic_vault_without_anthropic_row_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The vault only holds a github-copilot login, which no OpenScientist
        # provider maps to; the anthropic provider has no credential to sign with.
        vault = make_vault(
            tmp_path,
            {
                "github-copilot": {
                    "access": "vault-copilot-access",
                    "refresh": "vault-copilot-access",
                    "expires": _fresh_ms(),
                    "apiEndpoint": "https://copilot-api.example-corp.com",
                }
            },
        )
        monkeypatch.setenv("OPENSCIENTIST_PROVIDER", "anthropic")
        monkeypatch.setenv("OPENSCIENTIST_HARNESS", "omp")
        monkeypatch.setenv("OMP_AUTH_HOST_PATH", str(vault.parent))
        clear_settings_cache()
        provider = provider_class("anthropic")()
        error = provider.prelaunch_routing_error()
        assert error is not None
