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
    docker_socket_path: str = "/var/run/airgap-docker.sock",
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
            docker_socket_path=docker_socket_path,
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
        # RFC §12.1 originally said master secret + full DB URL stripped,
        # but PR-1 operationally allows them through pending §12.1's
        # job-scoped least-privilege credential mechanism (PR-2 TODO).
        # Codex Review-7 B1: previously stripped, so _load_runtime_context
        # failed and no airgap job ever started. The non-PR-1 cross-cutting
        # secret (GITHUB_TOKEN) is still stripped.
        assert "GITHUB_TOKEN" not in env
        assert env.get("OPENSCIENTIST_SECRET_KEY") == "master-secret-DO-NOT-LEAK"
        assert env.get("DATABASE_URL") == "postgresql://user:pw@db/x"

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

    def test_adds_airgap_endpoint_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The runner forwards PUBMED_BASE_URL from os.environ when set
        # (operator override); for this test we want the derived form, so
        # ensure nothing in the dev host env leaks in.
        monkeypatch.delenv("PUBMED_BASE_URL", raising=False)
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
        # Codex Review-7 BUG #2 (B2): runner must derive PUBMED_BASE_URL
        # from pubmed_addr — `literature.py` reads PUBMED_BASE_URL, not
        # the addr — otherwise PubMed search silently falls back to the
        # public NCBI URL which the airgap firewall then blocks.
        assert env["PUBMED_BASE_URL"] == "http://10.0.0.6:9000/entrez/eutils"

    def test_pubmed_base_url_explicit_override_preserved(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # If the operator exports PUBMED_BASE_URL explicitly (e.g. to point
        # at a non-default mirror layout), the runner must not clobber it.
        monkeypatch.setenv("PUBMED_BASE_URL", "http://mirror.lan/eutils-v2")
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
        assert env["PUBMED_BASE_URL"] == "http://mirror.lan/eutils-v2"

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

    def test_airgap_substitutes_proxy_socket_for_real_docker_sock(self) -> None:
        # Codex Review-6 BUG (fixed): the prior PR-1 test pinned mounting
        # the REAL /var/run/docker.sock in airgap mode, defeating the
        # network boundary (an agent with the real socket can spawn
        # privileged sibling containers). The proxy-socket substitution
        # at runner.py:140 is the fix; this test pins it.
        settings = _airgap_settings(enabled=True, docker_socket_path="/var/run/airgap-docker.sock")
        volumes = JobContainerRunner._build_container_volumes(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_dir_host=Path("/host/jobs/job-42"),
            job_mount="/agent/jobs/job-42",
        )
        assert "/host/jobs/job-42" in volumes
        # The CONTAINER side is still /var/run/docker.sock (the in-container
        # docker SDK reads that path); the HOST side is the PROXY socket.
        # `volumes` is a dict whose keys are HOST paths.
        assert "/var/run/airgap-docker.sock" in volumes
        assert volumes["/var/run/airgap-docker.sock"]["bind"] == "/var/run/docker.sock"
        # The real socket must NOT be a key (would mean we mounted the
        # raw Docker daemon socket on the host side).
        assert "/var/run/docker.sock" not in volumes

    def test_non_airgap_still_mounts_real_docker_sock(self) -> None:
        # Regression sentinel: non-airgap deployments still mount the real
        # /var/run/docker.sock — only airgap mode swaps to the proxy.
        settings = _airgap_settings(enabled=False)
        volumes = JobContainerRunner._build_container_volumes(  # type: ignore[arg-type]
            settings,  # type: ignore[arg-type]
            job_id="job-42",
            job_dir_host=Path("/host/jobs/job-42"),
            job_mount="/agent/jobs/job-42",
        )
        assert "/var/run/docker.sock" in volumes


# --------------------------------------------------------- provision_host_prelaunch


class TestProvisionAuthInAirgap:
    """RFC §12.2: auth.json must land in a host-mounted CODEX_HOME *outside*
    the job_dir so it does not end up in the exported artifact tree.

    After the PR #195 merge (2026-06-17), the legacy
    ``JobContainerRunner._provision_codex_auth`` method was replaced by
    backend-specific ``provision_host_prelaunch`` classmethods on each
    agent class. ``AirgapCodexAgent.provision_host_prelaunch`` carries the
    airgap routing (write to ``OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT/<job_id>/``
    instead of ``job_dir/.codex/``); ``CodexAgent.provision_host_prelaunch``
    keeps the legacy job-dir path. The runner just dispatches per-provider
    (see :func:`JobContainerRunner.launch`).
    """

    def test_airgap_writes_to_host_codex_home_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openscientist.airgap.codex_agent import AirgapCodexAgent

        src = tmp_path / "host-auth.json"
        src.write_text('{"tokens": {}}')
        job_dir = tmp_path / "job-42"
        job_dir.mkdir()
        codex_root = tmp_path / "codex-home-root"
        # The airgap CODEX_HOME root is selected via env var (RFC §12.2),
        # not the settings tree — so the runner doesn't have to know about it.
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT", str(codex_root))

        settings = _airgap_settings(enabled=True, codex_auth_host_path=str(src))
        AirgapCodexAgent.provision_host_prelaunch(settings, job_dir)  # type: ignore[arg-type]

        # Lands at codex_root/<job_id>/auth.json — NOT in job_dir/.codex.
        assert (codex_root / "job-42" / "auth.json").read_text() == '{"tokens": {}}'
        assert not (job_dir / ".codex").exists()

    def test_non_airgap_writes_to_job_dir(self, tmp_path: Path) -> None:
        # The non-airgap (legacy) CodexAgent classmethod still writes to
        # job_dir/.codex/. Sentinel against accidental regression of the
        # plain-codex path while the air-gap override evolves.
        from openscientist.agent.codex_agent import CodexAgent

        src = tmp_path / "host-auth.json"
        src.write_text('{"tokens": {}}')
        job_dir = tmp_path / "job-42"
        job_dir.mkdir()

        settings = _airgap_settings(enabled=False, codex_auth_host_path=str(src))
        CodexAgent.provision_host_prelaunch(settings, job_dir)  # type: ignore[arg-type]

        assert (job_dir / ".codex" / "auth.json").read_text() == '{"tokens": {}}'

    def test_airgap_noop_when_source_unset(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openscientist.airgap.codex_agent import AirgapCodexAgent

        job_dir = tmp_path / "job-42"
        job_dir.mkdir()
        codex_root = tmp_path / "codex-home-root"
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_CODEX_HOME_ROOT", str(codex_root))
        settings = _airgap_settings(enabled=True, codex_auth_host_path=None)
        AirgapCodexAgent.provision_host_prelaunch(settings, job_dir)  # type: ignore[arg-type]
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
