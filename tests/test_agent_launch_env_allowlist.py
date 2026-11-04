"""Every env key the web puts into an agent launch is one the launcher allows.

The web builds the launch env from the provider settings and the per-run
values, and the launcher refuses the whole launch on the first key outside
its allow-list. These tests build that env for every provider and harness
with every provider setting filled in, with and without the host login files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from openscientist.job_container.runner import JobContainerRunner
from openscientist.job_container.secrets import RunCredentials
from openscientist.providers import get_provider, provider_ids
from openscientist.settings import (
    _VALID_HARNESSES,
    ProviderSettings,
    clear_settings_cache,
    get_settings,
)
from openscientist_launcher.validation import AGENT_ENV_ALLOWLIST
from tests.test_provider_vertex import write_service_account

JOB_ID = "11111111-2222-3333-4444-555555555555"

#: A model id each provider's settings validation accepts; the rest take any name.
_MODEL_IDS = {
    "anthropic": "claude-sonnet-4-5",
    "cborg": "claude-sonnet-4-5",
    "vertex": "claude-sonnet-4-5@20250929",
    "bedrock": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
}

_HOST_LOGINS = ("CODEX_AUTH_HOST_PATH", "OMP_AUTH_HOST_PATH")

#: The combinations the web refuses before it asks for any launch.
_REFUSED = {("openai", "omp", True)}


def _filled_value(name: str, annotation: object, tmp_path: Path) -> str:
    """A value the setting accepts, for a field of this name and type."""
    if "int" in str(annotation):
        return "65536"
    if name.endswith("_base_url"):
        return "http://model.example.test:8000/v1"
    path = tmp_path / name
    if name == "google_application_credentials":
        write_service_account(path)
        return str(path)
    if name.endswith("_path"):
        path.write_text("{}", encoding="utf-8")
        return str(path)
    return f"filled-{name}"


@pytest.fixture
def every_setting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every provider setting, and every optional launch input, set in the env."""
    for name, field in ProviderSettings.model_fields.items():
        if field.alias and name not in {"provider_id", "harness"}:
            monkeypatch.setenv(field.alias, _filled_value(name, field.annotation, tmp_path))
    monkeypatch.setenv("OPENSCIENTIST_CODEX_TURN_TIMEOUT", "1800")
    monkeypatch.setenv("OPENSCIENTIST_HOST_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("PHENIX_HOST_PATH", str(tmp_path / "phenix"))


@pytest.mark.usefixtures("every_setting")
@pytest.mark.parametrize("host_logins", [True, False])
@pytest.mark.parametrize("harness", _VALID_HARNESSES)
@pytest.mark.parametrize("provider_id", provider_ids())
@pytest.mark.parametrize("run_mode", ["discovery", "report_only", "chat"])
@pytest.mark.parametrize("airgapped", [False, True])
def test_launch_env_keys_are_all_allowed(
    monkeypatch: pytest.MonkeyPatch,
    provider_id: str,
    harness: str,
    host_logins: bool,
    run_mode: str,
    airgapped: bool,
) -> None:
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", provider_id)
    monkeypatch.setenv("OPENSCIENTIST_HARNESS", harness)
    monkeypatch.setenv("OPENSCIENTIST_MODEL", _MODEL_IDS.get(provider_id, "filled-model"))
    if not host_logins:
        for key in _HOST_LOGINS:
            monkeypatch.delenv(key)
    clear_settings_cache()
    try:
        refusal = get_provider().prelaunch_routing_error()
        if refusal is not None:
            assert (provider_id, harness, host_logins) in _REFUSED, refusal
            return
        settings = get_settings()
        credentials = RunCredentials(
            job_id=JOB_ID,
            kind="chat" if run_mode == "chat" else "discovery",
            llm_placeholder=f"{JOB_ID}.placeholder",
            exec_token=f"{JOB_ID}.exec",
        )
        env = JobContainerRunner._build_container_environment(
            settings,
            job_id=JOB_ID,
            job_mount=f"/app/jobs/{JOB_ID}",
            provider_env=JobContainerRunner._provider_environment(settings, credentials),
            database_url="postgresql://agent@db/openscientist",
            exec_token=credentials.exec_token,
            airgapped=airgapped,
            run_mode=run_mode,
        )
    finally:
        clear_settings_cache()
    assert sorted(set(env) - AGENT_ENV_ALLOWLIST) == []
