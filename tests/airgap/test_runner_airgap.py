"""Tests for the air-gap branches in :class:`JobContainerRunner`."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from openscientist.job_container.runner import (
    _AIRGAP_CODEX_HOME_ROOT_DEFAULT,
    JobContainerRunner,
)


def _airgap_settings(
    *,
    enabled: bool,
    provider_id: str = "azure-openai",
    provider_env: dict[str, str] | None = None,
    llm_addr: str = "10.0.0.5:8443",
    pubmed_addr: str = "10.0.0.6:9000",
    codex_home_root: str | None = None,
    codex_auth_host_path: str | None = None,
    google_application_credentials: str | None = None,
    host_project_dir: str | None = None,
    phenix_host_path: str | None = None,
) -> SimpleNamespace:
    """Settings stand-in covering only the attributes the runner touches.

    The legacy ``_make_settings`` helpers in ``test_job_container.py`` test
    the non-airgap path; this one shapes the settings for the new airgap
    branches.
    """
    if provider_env is None:
        provider_env = {}
    provider = SimpleNamespace(
        provider_id=provider_id,
        google_application_credentials=google_application_credentials,
        gcp_credentials_host_path=None,
        codex_auth_host_path=codex_auth_host_path,
    )
    provider.get_container_env_vars = lambda: provider_env  # type: ignore[attr-defined]
    return SimpleNamespace(
        container=SimpleNamespace(
            host_project_dir=host_project_dir,
            container_app_dir="/app",
            agent_network=None,
            agent_memory="8g",
            agent_cpu=2.0,
            agent_platform=None,
            agent_image="agent:latest",
        ),
        provider=provider,
        database=SimpleNamespace(effective_database_url="postgresql://user:pw@db/x"),
        phenix=SimpleNamespace(phenix_host_path=phenix_host_path),
        secret_key="master-secret-DO-NOT-LEAK",
        airgap=SimpleNamespace(
            enabled=enabled,
            llm_addr=llm_addr,
            pubmed_addr=pubmed_addr,
            codex_home_root=codex_home_root,
        ),
    )


# --------------------------------------------------------- _airgap_codex_home_paths


class TestAirgapCodexHomePaths:
    def test_default_root(self) -> None:
        settings = _airgap_settings(enabled=True)
        host, container = JobContainerRunner._airgap_codex_home_paths(settings, "job-42")  # type: ignore[arg-type]
        assert host == container  # by default they match
        assert host.parent == Path(_AIRGAP_CODEX_HOME_ROOT_DEFAULT)
        assert host.name == "job-42"

    def test_override(self, tmp_path: Path) -> None:
        settings = _airgap_settings(enabled=True, codex_home_root=str(tmp_path / "tmpfs"))
        host, _ = JobContainerRunner._airgap_codex_home_paths(settings, "job-42")  # type: ignore[arg-type]
        assert host == tmp_path / "tmpfs" / "job-42"


# --------------------------------------------------------- _build_container_environment


class TestEnvFilteringInAirgap:
    """When airgap is enabled, ``_build_container_environment`` must filter
    out cross-provider creds and master secrets while preserving the base
    path/runtime vars."""

    def _polluted_env(self) -> dict[str, str]:
        return {
            "OPENSCIENTIST_PROVIDER": "azure-openai",
            "AZURE_OPENAI_API_KEY": "active-secret",
            "AZURE_OPENAI_RESOURCE": "myaoai",
            # Cross-provider noise from get_container_env_vars when multiple
            # provider creds are configured in the host env.
            "OPENAI_API_KEY": "should-be-stripped",
            "ANTHROPIC_API_KEY": "should-be-stripped",
            "AWS_ACCESS_KEY_ID": "should-be-stripped",
            "GITHUB_TOKEN": "should-be-stripped",
        }

    def test_strips_inactive_provider_creds(self) -> None:
        settings = _airgap_settings(
            enabled=True, provider_id="azure-openai", provider_env=self._polluted_env()
        )
        env = JobContainerRunner._build_container_environment(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_mount="/agent/jobs/job-42",  # type: ignore[arg-type]
        )
        # Active provider's creds survive.
        assert env["AZURE_OPENAI_API_KEY"] == "active-secret"
        assert env["AZURE_OPENAI_RESOURCE"] == "myaoai"
        # Other providers' creds are gone.
        assert "OPENAI_API_KEY" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "AWS_ACCESS_KEY_ID" not in env
        assert "GITHUB_TOKEN" not in env

    def test_strips_master_secret_and_database_url(self) -> None:
        settings = _airgap_settings(
            enabled=True, provider_id="azure-openai", provider_env=self._polluted_env()
        )
        env = JobContainerRunner._build_container_environment(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_mount="/agent/jobs/job-42",  # type: ignore[arg-type]
        )
        # RFC §12.1: master secret + full DB URL stripped from agent env.
        assert "OPENSCIENTIST_SECRET_KEY" not in env
        assert "DATABASE_URL" not in env
        # ... and 'master-secret-DO-NOT-LEAK' must not appear in any value.
        for value in env.values():
            assert "master-secret-DO-NOT-LEAK" not in value
            assert "postgresql://user:pw@db/x" not in value

    def test_preserves_base_runtime_vars(self) -> None:
        # PHENIX_PATH and OPENSCIENTIST_HOST_PROJECT_DIR are paths/config,
        # not creds — they must survive the filter.
        settings = _airgap_settings(
            enabled=True,
            provider_id="azure-openai",
            provider_env=self._polluted_env(),
            host_project_dir="/home/op/openscientist",
            phenix_host_path="/Applications/phenix-1.21.2",
        )
        env = JobContainerRunner._build_container_environment(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_mount="/agent/jobs/job-42",  # type: ignore[arg-type]
        )
        assert env["OPENSCIENTIST_HOST_PROJECT_DIR"] == "/home/op/openscientist"
        assert env["OPENSCIENTIST_CONTAINER_APP_DIR"] == "/agent"
        assert env["PHENIX_PATH"] == "/opt/phenix"

    def test_preserves_job_identity_vars(self) -> None:
        settings = _airgap_settings(
            enabled=True, provider_id="azure-openai", provider_env=self._polluted_env()
        )
        env = JobContainerRunner._build_container_environment(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_mount="/agent/jobs/job-42",  # type: ignore[arg-type]
        )
        assert env["JOB_ID"] == "job-42"
        assert env["JOB_DIR"] == "/agent/jobs/job-42"

    def test_adds_airgap_endpoint_vars(self) -> None:
        settings = _airgap_settings(
            enabled=True,
            provider_id="azure-openai",
            provider_env=self._polluted_env(),
            llm_addr="10.0.0.5:8443",
            pubmed_addr="10.0.0.6:9000",
        )
        env = JobContainerRunner._build_container_environment(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_mount="/agent/jobs/job-42",  # type: ignore[arg-type]
        )
        assert env["OPENSCIENTIST_AIR_GAPPED"] == "1"
        assert env["OPENSCIENTIST_AIRGAP_LLM_ADDR"] == "10.0.0.5:8443"
        assert env["OPENSCIENTIST_AIRGAP_PUBMED_ADDR"] == "10.0.0.6:9000"

    def test_adds_codex_home_root_for_subclass(self) -> None:
        # The AirgapCodexAgent reads this env var to relocate _codex_home();
        # the runner must set it to the container-side path of the bind
        # mount so the agent finds auth.json there.
        settings = _airgap_settings(enabled=True, codex_home_root=None)
        env = JobContainerRunner._build_container_environment(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_mount="/agent/jobs/job-42",  # type: ignore[arg-type]
        )
        # Default root (the per-job suffix is appended by the agent itself).
        assert env["OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT"] == str(
            Path(_AIRGAP_CODEX_HOME_ROOT_DEFAULT)
        )


# --------------------------------------------------------- _build_container_volumes


class TestVolumesInAirgap:
    def test_airgap_adds_codex_home_bind_mount(self) -> None:
        settings = _airgap_settings(enabled=True)
        volumes = JobContainerRunner._build_container_volumes(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_dir_host=Path("/host/jobs/job-42"),
            job_mount="/agent/jobs/job-42",
        )
        host_path = f"{_AIRGAP_CODEX_HOME_ROOT_DEFAULT}/job-42"
        assert host_path in volumes
        assert volumes[host_path] == {
            "bind": f"{_AIRGAP_CODEX_HOME_ROOT_DEFAULT}/job-42",
            "mode": "rw",
        }

    def test_non_airgap_omits_codex_home_mount(self) -> None:
        settings = _airgap_settings(enabled=False)
        volumes = JobContainerRunner._build_container_volumes(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_dir_host=Path("/host/jobs/job-42"),
            job_mount="/agent/jobs/job-42",
        )
        # Sentinel: no airgap mount when disabled.
        for path in volumes:
            assert "openscientist-codex-home" not in path

    def test_job_dir_and_docker_socket_still_present_in_airgap(self) -> None:
        # PR-1 keeps the existing docker.sock mount; the airgap-only socket
        # proxy is wired in container_manager.py, a separate edit.
        settings = _airgap_settings(enabled=True)
        volumes = JobContainerRunner._build_container_volumes(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_dir_host=Path("/host/jobs/job-42"),
            job_mount="/agent/jobs/job-42",
        )
        assert "/host/jobs/job-42" in volumes
        assert "/var/run/docker.sock" in volumes


# --------------------------------------------------------- _provision_codex_auth


class TestProvisionAuthInAirgap:
    """RFC §12.2: auth.json must land in a host-mounted CODEX_HOME *outside*
    the job_dir so it does not end up in the exported artifact tree."""

    def test_airgap_writes_to_host_codex_home_root(self, tmp_path: Path) -> None:
        src = tmp_path / "host-auth.json"
        src.write_text('{"tokens": {}}')
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        codex_root = tmp_path / "codex-home-root"

        settings = _airgap_settings(
            enabled=True,
            codex_auth_host_path=str(src),
            codex_home_root=str(codex_root),
        )
        JobContainerRunner._provision_codex_auth(settings, "job-42", job_dir)  # type: ignore[arg-type]

        # Lands at codex_root/<job_id>/auth.json — NOT in job_dir/.codex.
        assert (codex_root / "job-42" / "auth.json").read_text() == '{"tokens": {}}'
        assert not (job_dir / ".codex").exists()

    def test_non_airgap_writes_to_job_dir(self, tmp_path: Path) -> None:
        # Regression sentinel for the legacy path.
        src = tmp_path / "host-auth.json"
        src.write_text('{"tokens": {}}')
        job_dir = tmp_path / "job"
        job_dir.mkdir()

        settings = _airgap_settings(enabled=False, codex_auth_host_path=str(src))
        JobContainerRunner._provision_codex_auth(settings, "job-42", job_dir)  # type: ignore[arg-type]

        assert (job_dir / ".codex" / "auth.json").read_text() == '{"tokens": {}}'

    def test_airgap_noop_when_source_unset(self, tmp_path: Path) -> None:
        job_dir = tmp_path / "job"
        job_dir.mkdir()
        codex_root = tmp_path / "codex-home-root"
        settings = _airgap_settings(
            enabled=True, codex_auth_host_path=None, codex_home_root=str(codex_root)
        )
        JobContainerRunner._provision_codex_auth(settings, "job-42", job_dir)  # type: ignore[arg-type]
        # No auth = no file = no work done. The codex_root dir is not even
        # created (the airgap branch returns before mkdir).
        assert not codex_root.exists()


@pytest.mark.parametrize("provider_id", ["azure-openai", "anthropic", "openai", "foundry"])
def test_filtering_works_for_each_supported_provider(provider_id: str) -> None:
    """Smoke check across the 4 providers env_allowlist actually supports.
    Bedrock and Vertex are unsupported per RFC §19 OQ#2 and tested elsewhere."""
    settings = _airgap_settings(
        enabled=True,
        provider_id=provider_id,
        provider_env={
            "OPENSCIENTIST_PROVIDER": provider_id,
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "OPENAI_API_KEY": "openai-secret",
            "AZURE_OPENAI_API_KEY": "azure-secret",
            "ANTHROPIC_FOUNDRY_API_KEY": "foundry-secret",
        },
    )
    env = JobContainerRunner._build_container_environment(  # type: ignore[arg-type]
        settings,  # type: ignore[arg-type]
        job_id="job-42",
        job_mount="/agent/jobs/job-42",  # type: ignore[arg-type]
    )
    # OPENSCIENTIST_PROVIDER always survives (it's in BASE_AIRGAP_ENV).
    assert env["OPENSCIENTIST_PROVIDER"] == provider_id
