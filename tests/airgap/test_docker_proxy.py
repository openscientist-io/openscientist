"""Tests for the air-gap Docker socket selection."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from openscientist.airgap.docker_proxy import docker_base_url_for_airgap
from openscientist.container_manager import ContainerManager
from openscientist.settings import AirgapSettings

# --------------------------------------------------------- docker_base_url_for_airgap


class TestDockerBaseUrl:
    def test_default_path(self) -> None:
        s = SimpleNamespace(
            airgap=SimpleNamespace(docker_socket_path="/var/run/airgap-docker.sock")
        )
        assert docker_base_url_for_airgap(s) == "unix:///var/run/airgap-docker.sock"

    def test_custom_path(self) -> None:
        s = SimpleNamespace(airgap=SimpleNamespace(docker_socket_path="/tmp/custom-proxy.sock"))
        assert docker_base_url_for_airgap(s) == "unix:///tmp/custom-proxy.sock"


# --------------------------------------------------------- AirgapSettings.docker_socket_path


class TestSocketPathValidation:
    def test_default_is_safe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_LLM_ADDR", "10.0.0.5:8443")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_PUBMED_ADDR", "10.0.0.6:9000")
        monkeypatch.delenv("OPENSCIENTIST_AIRGAP_DOCKER_SOCKET_PATH", raising=False)
        s = AirgapSettings(_env_file=None)
        # Default is the airgap proxy socket, not the real one.
        assert s.docker_socket_path == "/var/run/airgap-docker.sock"

    def test_real_docker_sock_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_LLM_ADDR", "10.0.0.5:8443")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_PUBMED_ADDR", "10.0.0.6:9000")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_DOCKER_SOCKET_PATH", "/var/run/docker.sock")
        with pytest.raises(ValidationError, match="real Docker socket"):
            AirgapSettings(_env_file=None)

    def test_custom_path_accepted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OPENSCIENTIST_AIR_GAPPED", "true")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_LLM_ADDR", "10.0.0.5:8443")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_PUBMED_ADDR", "10.0.0.6:9000")
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_DOCKER_SOCKET_PATH", "/tmp/my-proxy.sock")
        s = AirgapSettings(_env_file=None)
        assert s.docker_socket_path == "/tmp/my-proxy.sock"

    def test_validation_only_when_enabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Non-airgap deployments still use docker.from_env() which resolves
        # to /var/run/docker.sock; AirgapSettings.docker_socket_path is
        # ignored in that case. The validator must not fire.
        for var in (
            "OPENSCIENTIST_AIR_GAPPED",
            "OPENSCIENTIST_AIRGAP_LLM_ADDR",
            "OPENSCIENTIST_AIRGAP_PUBMED_ADDR",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("OPENSCIENTIST_AIRGAP_DOCKER_SOCKET_PATH", "/var/run/docker.sock")
        # Must not raise — airgap disabled.
        s = AirgapSettings(_env_file=None)
        assert s.enabled is False


# --------------------------------------------------------- ContainerManager.client


class TestContainerManagerClientSelection:
    """The lazy-loaded Docker client dispatches on settings.airgap.enabled —
    proxy socket in airgap, ``docker.from_env()`` otherwise."""

    def _make_settings(self, *, airgap_enabled: bool) -> SimpleNamespace:
        return SimpleNamespace(
            container=SimpleNamespace(
                executor_image="executor:latest",
                executor_memory="2g",
                executor_cpu=0.5,
                executor_timeout=120,
            ),
            airgap=SimpleNamespace(
                enabled=airgap_enabled,
                docker_socket_path="/var/run/airgap-docker.sock",
            ),
        )

    def test_airgap_disabled_uses_from_env(self) -> None:
        # Sentinel: the non-airgap path is unchanged.
        with patch(
            "openscientist.container_manager.get_settings",
            return_value=self._make_settings(airgap_enabled=False),
        ):
            cm = ContainerManager()
            with (
                patch("docker.from_env") as mock_from_env,
                patch("docker.DockerClient") as mock_docker_client,
            ):
                mock_from_env.return_value = MagicMock()
                _ = cm.client
            mock_from_env.assert_called_once()
            mock_docker_client.assert_not_called()

    def test_airgap_enabled_uses_proxy_socket(self) -> None:
        with patch(
            "openscientist.container_manager.get_settings",
            return_value=self._make_settings(airgap_enabled=True),
        ):
            cm = ContainerManager()
            with (
                patch("docker.from_env") as mock_from_env,
                patch("docker.DockerClient") as mock_docker_client,
            ):
                mock_docker_client.return_value = MagicMock()
                _ = cm.client
            mock_from_env.assert_not_called()
            mock_docker_client.assert_called_once_with(
                base_url="unix:///var/run/airgap-docker.sock"
            )

    def test_airgap_enabled_uses_custom_proxy_path(self) -> None:
        settings = self._make_settings(airgap_enabled=True)
        settings.airgap.docker_socket_path = "/run/airgap-proxy.sock"
        with patch("openscientist.container_manager.get_settings", return_value=settings):
            cm = ContainerManager()
            with patch("docker.from_env"), patch("docker.DockerClient") as mock_docker_client:
                mock_docker_client.return_value = MagicMock()
                _ = cm.client
            mock_docker_client.assert_called_once_with(base_url="unix:///run/airgap-proxy.sock")
