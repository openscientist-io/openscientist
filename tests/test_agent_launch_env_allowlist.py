"""Every env key the web puts into an agent launch is one the launcher allows.

The web builds the launch env from the provider settings and the per-run
values, and the launcher refuses the whole launch on the first key outside
its allow-list. These tests build that env for every provider and harness
with every provider setting filled in, one of each set of alternatives, with
and without the host login files, and again with each provider in every one
of its auth variants, so a branch that only shows up when one credential is
missing cannot hide a key. The drift test takes the union of every key those
combinations emit and refuses an allow-list key that no combination sends, so
the list cannot widen past what the web actually puts into a container.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openscientist.job_container.runner import JobContainerRunner
from openscientist.job_container.secrets import RunCredentials
from openscientist.providers import get_provider, provider_class, provider_ids
from openscientist.settings import (
    _VALID_HARNESSES,
    ProviderSettings,
    clear_settings_cache,
    get_settings,
)
from openscientist_launcher.validation import AGENT_ENV_ALLOWLIST
from tests.omp_vault import make_jwt, make_vault, now_ms
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


def _fill_every_setting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every provider setting, and every optional launch input, set in the env."""
    # A harness export the Settings model would parse as its agent section.
    monkeypatch.delenv("AGENT", raising=False)
    for name, field in ProviderSettings.model_fields.items():
        if field.alias and name not in {"provider_id", "harness"}:
            monkeypatch.setenv(field.alias, _filled_value(name, field.annotation, tmp_path))
    monkeypatch.setenv("OPENSCIENTIST_CODEX_TURN_TIMEOUT", "1800")
    monkeypatch.setenv("OPENSCIENTIST_HOST_PROJECT_DIR", str(tmp_path / "project"))
    monkeypatch.setenv("PHENIX_HOST_PATH", str(tmp_path / "phenix"))


def _launch_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    provider_id: str,
    harness: str = "auto",
    host_logins: bool = True,
    run_mode: str = "discovery",
    airgapped: bool = False,
    overrides: dict[str, str | None] | None = None,
    variant_name: str = "",
) -> dict[str, str] | str:
    """The agent launch env one web combination builds through the web's own
    path, or the refusal when the web refuses the launch before it asks the
    launcher. Every call starts from the all-filled-in config, so a
    combination is never shaped by the one that ran before it."""
    _fill_every_setting(monkeypatch, tmp_path)
    monkeypatch.setenv("OPENSCIENTIST_PROVIDER", provider_id)
    monkeypatch.setenv("OPENSCIENTIST_HARNESS", harness)
    monkeypatch.setenv("OPENSCIENTIST_MODEL", _MODEL_IDS.get(provider_id, "filled-model"))
    for key in _HOST_LOGINS:
        if host_logins:
            path = tmp_path / key.lower()
            if not path.exists():
                path.write_text("{}", encoding="utf-8")
            monkeypatch.setenv(key, str(path))
        else:
            monkeypatch.delenv(key)
    for field, value in (overrides or {}).items():
        alias = _env_name(field)
        if value is None:
            monkeypatch.delenv(alias, raising=False)
        elif value == _VAULT:
            vault = tmp_path / f"vault-{provider_id}-{variant_name}"
            make_vault(vault, {_VAULT_PROVIDER[provider_id]: _vault_credential(provider_id)})
            monkeypatch.setenv(alias, str(vault))
        else:
            monkeypatch.setenv(alias, value)
    _keep_first_alternative(monkeypatch, provider_id)
    clear_settings_cache()
    try:
        refusal = get_provider().prelaunch_routing_error()
        if refusal is not None:
            return refusal
        settings = get_settings()
        credentials = RunCredentials(
            job_id=JOB_ID,
            kind="chat" if run_mode == "chat" else "discovery",
            llm_placeholder=f"{JOB_ID}.placeholder",
            exec_token=f"{JOB_ID}.exec",
        )
        return JobContainerRunner._build_container_environment(
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


def _keep_first_alternative(monkeypatch: pytest.MonkeyPatch, provider_id: str) -> None:
    """Unset every alternative after the first one set, since a provider refuses several together."""
    for group in provider_class(provider_id).setup_alternatives():
        set_members = [member for member in group if any(os.environ.get(a) for a in member)]
        for member in set_members[1:]:
            for alias in member:
                monkeypatch.delenv(alias, raising=False)


@pytest.mark.parametrize("host_logins", [True, False])
@pytest.mark.parametrize("harness", _VALID_HARNESSES)
@pytest.mark.parametrize("provider_id", provider_ids())
@pytest.mark.parametrize("run_mode", ["discovery", "report_only", "chat"])
@pytest.mark.parametrize("airgapped", [False, True])
def test_launch_env_keys_are_all_allowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_id: str,
    harness: str,
    host_logins: bool,
    run_mode: str,
    airgapped: bool,
) -> None:
    result = _launch_env(
        monkeypatch,
        tmp_path,
        provider_id=provider_id,
        harness=harness,
        host_logins=host_logins,
        run_mode=run_mode,
        airgapped=airgapped,
    )
    if isinstance(result, str):
        assert (provider_id, harness, host_logins) in _REFUSED, result
        return
    assert sorted(set(result) - AGENT_ENV_ALLOWLIST) == []


#: The OMP_AUTH_HOST_PATH override that points the setting at a real omp
#: vault the test builds, instead of the placeholder file the fixture writes.
_VAULT = "<vault>"

#: The vault provider name the two vault-signed providers store in agent.db.
_VAULT_PROVIDER = {"anthropic": "anthropic", "openai": "openai-codex"}

#: One auth variant per provider, mirrored from the auth-choice branches in
#: src/openscientist/providers/<provider>.py. An override unsets a setting
#: (None) or pins the vault sentinel, so each variant exercises one branch on
#: top of the all-filled-in config. Foundry's API-key vs Entra auth and the
#: single-method providers emit the same container env in every branch, so a
#: variant with no overrides is their whole table.
_VARIANTS: dict[str, tuple[tuple[str, dict[str, str | None]], ...]] = {
    # The vault serves only when neither the api key nor the OAuth token is set.
    "anthropic": (
        (
            "api_key",
            {
                "anthropic_auth_token": None,
                "anthropic_base_url": None,
                "claude_code_oauth_token": None,
                "omp_auth_host_path": None,
            },
        ),
        (
            "oauth",
            {
                "anthropic_api_key": None,
                "anthropic_auth_token": None,
                "anthropic_base_url": None,
                "omp_auth_host_path": None,
            },
        ),
        (
            "vault",
            {
                "anthropic_api_key": None,
                "anthropic_auth_token": None,
                "anthropic_base_url": None,
                "claude_code_oauth_token": None,
                "omp_auth_host_path": _VAULT,
            },
        ),
    ),
    # The gateway token and URL are required, so the variants toggle only the
    # Anthropic-side fields the shared container env reads.
    "cborg": (("gateway", {"anthropic_api_key": None, "claude_code_oauth_token": None}),),
    "vertex": (("service_account", {}),),
    # The bearer token takes the static-header branch, and the access keys the
    # SigV4 branch (proxy_env_overrides).
    "bedrock": (
        ("sigv4_access_key", {"aws_bearer_token_bedrock": None, "aws_profile": None}),
        (
            "bearer_token",
            {"aws_access_key_id": None, "aws_secret_access_key": None, "aws_profile": None},
        ),
    ),
    # Resource and base URL are mutually exclusive endpoint alternatives
    # (container_env and proxy_env_overrides).
    "foundry": (
        ("resource_api_key", {"anthropic_foundry_base_url": None}),
        ("base_url_api_key", {"anthropic_foundry_resource": None}),
    ),
    # The api key beats the host codex login, which beats the vault.
    "openai": (
        ("api_key", {"codex_auth_host_path": None, "omp_auth_host_path": None}),
        ("codex_login", {"openai_api_key": None, "omp_auth_host_path": None}),
        (
            "omp_vault",
            {
                "openai_api_key": None,
                "codex_auth_host_path": None,
                "omp_auth_host_path": _VAULT,
            },
        ),
    ),
    "azure-openai": (("api_key", {}),),
    "ollama": (("keyless", {}),),
    # The container carries the API-key placeholder only when the server has
    # a key (proxy_env_overrides).
    "vllm": (
        ("api_key", {}),
        ("keyless", {"vllm_api_key": None}),
    ),
    "llamacpp": (
        ("api_key", {}),
        ("keyless", {"llamacpp_api_key": None}),
    ),
}

#: The settings fields each provider's auth-choice logic reads that change the
#: launch env, mirrored from the same provider files. Every field must be
#: unset by at least one variant of its provider, so a branch cannot stay
#: covered only by the all-filled-in config.
_AUTH_FIELDS: dict[str, tuple[str, ...]] = {
    "anthropic": (
        "anthropic_api_key",
        "anthropic_auth_token",
        "anthropic_base_url",
        "claude_code_oauth_token",
        "omp_auth_host_path",
    ),
    "cborg": ("anthropic_api_key", "claude_code_oauth_token"),
    "vertex": (),
    "bedrock": ("aws_bearer_token_bedrock",),
    "foundry": ("anthropic_foundry_base_url", "anthropic_foundry_resource"),
    "openai": ("openai_api_key", "codex_auth_host_path", "omp_auth_host_path"),
    "azure-openai": (),
    "ollama": (),
    "vllm": ("vllm_api_key",),
    "llamacpp": ("llamacpp_api_key",),
}


def _env_name(field: str) -> str:
    """The env alias of a ProviderSettings field."""
    return ProviderSettings.model_fields[field].alias or field


def _vault_credential(provider_id: str) -> dict[str, object]:
    """A fresh fake credential row for the provider's vault login."""
    if provider_id == "openai":
        access = make_jwt(datetime.now(UTC) + timedelta(minutes=30))
    else:
        access = "sk-ant-oat01-vault-access"
    return {"access": access, "refresh": "vault-refresh", "expires": now_ms() + 3_600_000}


def test_the_auth_variant_table_covers_every_provider() -> None:
    assert set(_VARIANTS) == set(provider_ids()), "a provider is missing its auth variants"
    assert set(_AUTH_FIELDS) == set(provider_ids()), "a provider is missing its auth fields"
    for provider_id, fields in _AUTH_FIELDS.items():
        unset = {
            name
            for _, overrides in _VARIANTS[provider_id]
            for name, value in overrides.items()
            if value is None
        }
        missing = [name for name in fields if name not in unset]
        assert missing == [], f"{provider_id}: no variant unsets {missing}"


_VARIANT_CASES = [
    (provider_id, name, overrides)
    for provider_id, variants in _VARIANTS.items()
    for name, overrides in variants
]


@pytest.mark.parametrize(
    ("provider_id", "variant_name", "overrides"),
    _VARIANT_CASES,
    ids=[f"{provider_id}/{name}" for provider_id, name, _ in _VARIANT_CASES],
)
def test_auth_variant_launch_env_keys_are_all_allowed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider_id: str,
    variant_name: str,
    overrides: dict[str, str | None],
) -> None:
    result = _launch_env(
        monkeypatch,
        tmp_path,
        provider_id=provider_id,
        overrides=overrides,
        variant_name=variant_name,
    )
    assert isinstance(result, dict), f"{provider_id} {variant_name} is refused at launch: {result}"
    extra = sorted(set(result) - AGENT_ENV_ALLOWLIST)
    assert extra == [], f"{provider_id} {variant_name}: env keys the launcher refuses: {extra}"


def test_allowlist_keeps_only_keys_the_web_sends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The reverse of the launch tests. A key no combination this file builds
    puts into an agent launch env must not stay on the launcher's allow-list,
    since the web cannot route it through the launcher at all."""
    sent: set[str] = set()
    for provider_id in provider_ids():
        for harness in _VALID_HARNESSES:
            for host_logins in (True, False):
                for run_mode in ("discovery", "report_only", "chat"):
                    for airgapped in (False, True):
                        result = _launch_env(
                            monkeypatch,
                            tmp_path,
                            provider_id=provider_id,
                            harness=harness,
                            host_logins=host_logins,
                            run_mode=run_mode,
                            airgapped=airgapped,
                        )
                        if isinstance(result, dict):
                            sent |= set(result)
        for variant_name, overrides in _VARIANTS[provider_id]:
            result = _launch_env(
                monkeypatch,
                tmp_path,
                provider_id=provider_id,
                overrides=overrides,
                variant_name=variant_name,
            )
            if isinstance(result, dict):
                sent |= set(result)
    missing = sorted(AGENT_ENV_ALLOWLIST - sent)
    assert missing == [], f"allow-list keys the web never sends: {missing}"
