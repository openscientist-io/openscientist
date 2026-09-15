"""Tests for Vertex AI provider."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from openscientist.providers.base import AirgapEgress
from openscientist.providers.vertex import VertexProvider
from openscientist.settings import clear_settings_cache


class TestVertexProviderValidation:
    """Tests for Vertex AI provider configuration validation."""

    def test_valid_config(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            provider = VertexProvider()
            assert "vertex" in provider.display_name.lower()

    def test_airgap_egress_is_direct(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            posture = VertexProvider().airgap_egress()
        assert posture.mode is AirgapEgress.DIRECT
        assert ("us-east5-aiplatform.googleapis.com", 443) in posture.direct_endpoints
        assert ("oauth2.googleapis.com", 443) in posture.direct_endpoints

    def test_missing_creds_file_raises(self):
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
                "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/creds.json",
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            with pytest.raises(ValueError, match="not found"):
                VertexProvider()

    def test_missing_project_id_raises(self):
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.provider.anthropic_vertex_project_id = None
        mock_settings.provider.google_application_credentials = "/some/creds.json"
        mock_settings.provider.gcp_billing_account_id = "id"
        mock_settings.provider.cloud_ml_region = "us-east5"
        mock_settings.provider.model = "model"
        mock_settings.provider.vertex_region_claude_4_5_sonnet = None
        mock_settings.provider.vertex_region_claude_4_5_haiku = None

        with (
            patch("openscientist.providers.vertex.get_settings", return_value=mock_settings),
            patch("os.path.exists", return_value=True),
            pytest.raises(ValueError, match="ANTHROPIC_VERTEX_PROJECT_ID"),
        ):
            VertexProvider()

    def test_missing_credentials_raises(self):
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.provider.anthropic_vertex_project_id = "proj"
        mock_settings.provider.google_application_credentials = None
        mock_settings.provider.gcp_billing_account_id = "id"
        mock_settings.provider.cloud_ml_region = "us-east5"
        mock_settings.provider.model = "model"
        mock_settings.provider.vertex_region_claude_4_5_sonnet = None
        mock_settings.provider.vertex_region_claude_4_5_haiku = None

        with (
            patch("openscientist.providers.vertex.get_settings", return_value=mock_settings),
            pytest.raises(
                ValueError,
                match="GOOGLE_APPLICATION_CREDENTIALS",
            ),
        ):
            VertexProvider()

    def test_credentials_file_not_found_raises(self):
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
                "GOOGLE_APPLICATION_CREDENTIALS": "/does/not/exist/creds.json",
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            with pytest.raises(ValueError, match="not found"):
                VertexProvider()

    def test_optional_warnings_missing_model(self):
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.provider.anthropic_vertex_project_id = "proj"
        mock_settings.provider.google_application_credentials = "/some/creds.json"
        mock_settings.provider.gcp_billing_account_id = "id"
        mock_settings.provider.cloud_ml_region = "us-east5"
        mock_settings.provider.model = None
        mock_settings.provider.vertex_region_claude_4_5_sonnet = None
        mock_settings.provider.vertex_region_claude_4_5_haiku = None

        with (
            patch("openscientist.providers.vertex.get_settings", return_value=mock_settings),
            patch("os.path.exists", return_value=True),
        ):
            # Should not raise — optional warnings don't prevent init
            provider = VertexProvider()
            assert provider.display_name == "Vertex AI"


class TestVertexSetupEnvironment:
    """Tests for Vertex AI environment setup."""

    def test_setup_does_not_raise(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            provider = VertexProvider()
            provider.setup_environment()  # should just log, not raise

    def test_setup_clears_conflicting_vars(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "OPENSCIENTIST_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
                "CLAUDE_CODE_USE_BEDROCK": "1",
                "ANTHROPIC_API_KEY": "sk-test",
            },
        ):
            clear_settings_cache()
            provider = VertexProvider()
            provider.setup_environment()
            assert "CLAUDE_CODE_USE_BEDROCK" not in os.environ
            assert "ANTHROPIC_API_KEY" not in os.environ


def _mock_settings(
    creds: str,
    *,
    project: str | None = "proj-123",
    billing: str | None = "BILL-1234",
    region: str | None = "us-east5",
    sonnet: str | None = "us-east5",
    haiku: str | None = "us-east5",
    model: str | None = "claude-sonnet-4-6",
) -> MagicMock:
    mock_settings = MagicMock()
    mock_settings.provider.anthropic_vertex_project_id = project
    mock_settings.provider.google_application_credentials = creds
    mock_settings.provider.gcp_billing_account_id = billing
    mock_settings.provider.cloud_ml_region = region
    mock_settings.provider.vertex_region_claude_4_5_sonnet = sonnet
    mock_settings.provider.vertex_region_claude_4_5_haiku = haiku
    mock_settings.provider.model = model
    return mock_settings


class TestVertexClaudeCompatible:
    """Tests for the ClaudeCompatible family methods."""

    def _creds_file(self, tmp_path: Path) -> str:
        creds = tmp_path / "sa.json"
        creds.write_text("{}")
        return str(creds)

    def test_id_is_vertex(self, tmp_path: Path) -> None:
        settings = _mock_settings(self._creds_file(tmp_path))
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            assert VertexProvider().id == "vertex"

    def test_display_name_is_vertex_ai(self, tmp_path: Path) -> None:
        settings = _mock_settings(self._creds_file(tmp_path))
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            assert VertexProvider().display_name == "Vertex AI"

    def test_is_claude_compatible_and_provider(self, tmp_path: Path) -> None:
        from openscientist.providers.base import (
            ClaudeCompatible,
            CodexCompatible,
            Provider,
        )

        settings = _mock_settings(self._creds_file(tmp_path))
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            provider = VertexProvider()
        assert isinstance(provider, Provider)
        assert isinstance(provider, ClaudeCompatible)
        assert not isinstance(provider, CodexCompatible)

    def test_validate_required_config_ok(self, tmp_path: Path) -> None:
        settings = _mock_settings(self._creds_file(tmp_path))
        assert VertexProvider.required_config_errors(settings.provider) == []

    def test_validate_required_config_errors_when_unset(self, tmp_path: Path) -> None:
        unset = _mock_settings("", project=None, billing=None, region=None)
        unset.provider.google_application_credentials = None
        errors = VertexProvider.required_config_errors(unset.provider)
        assert any("ANTHROPIC_VERTEX_PROJECT_ID" in e for e in errors)
        assert any("GOOGLE_APPLICATION_CREDENTIALS" in e for e in errors)
        assert any("GCP_BILLING_ACCOUNT_ID" in e for e in errors)
        assert any("CLOUD_ML_REGION" in e for e in errors)

    def test_claude_sdk_env_full_config(self, tmp_path: Path) -> None:
        creds = self._creds_file(tmp_path)
        settings = _mock_settings(creds)
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            env = VertexProvider().claude_sdk_env()
        assert env == {
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_PROJECT_ID": "proj-123",
            "GCP_BILLING_ACCOUNT_ID": "BILL-1234",
            "CLOUD_ML_REGION": "us-east5",
            "VERTEX_REGION_CLAUDE_4_5_SONNET": "us-east5",
            "VERTEX_REGION_CLAUDE_4_5_HAIKU": "us-east5",
            "GOOGLE_APPLICATION_CREDENTIALS": creds,
        }

    def test_claude_sdk_env_omits_unset_optional_regions(self, tmp_path: Path) -> None:
        creds = self._creds_file(tmp_path)
        settings = _mock_settings(creds, sonnet=None, haiku=None)
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            env = VertexProvider().claude_sdk_env()
        assert env["CLAUDE_CODE_USE_VERTEX"] == "1"
        assert "VERTEX_REGION_CLAUDE_4_5_SONNET" not in env
        assert "VERTEX_REGION_CLAUDE_4_5_HAIKU" not in env
        assert env["ANTHROPIC_VERTEX_PROJECT_ID"] == "proj-123"

    def test_claude_model_name_uses_configured_model(self, tmp_path: Path) -> None:
        settings = _mock_settings(self._creds_file(tmp_path), model="claude-custom")
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            assert VertexProvider().claude_model_name() == "claude-custom"

    def test_claude_model_name_falls_back_to_vertex_default(self, tmp_path: Path) -> None:
        settings = _mock_settings(self._creds_file(tmp_path), model=None)
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            assert VertexProvider().claude_model_name() == "claude-sonnet-4-5@20250929"


class TestVertexRuntimeGuards:
    """Coverage for the cost + message defensive paths (audit Priority-7)."""

    def _valid(self, tmp_path: Path) -> MagicMock:
        creds = tmp_path / "sa.json"
        creds.write_text("{}")
        return _mock_settings(str(creds))

    def _provider(self, tmp_path: Path) -> VertexProvider:
        settings = self._valid(tmp_path)
        with patch("openscientist.providers.vertex.get_settings", return_value=settings):
            return VertexProvider()

    def test_get_cost_info_raises_without_credentials(self, tmp_path: Path) -> None:
        from openscientist.exceptions import ProviderError

        provider = self._provider(tmp_path)
        bad = self._valid(tmp_path)
        bad.provider.google_application_credentials = None
        with patch("openscientist.providers.vertex.get_settings", return_value=bad):
            with pytest.raises(ProviderError, match="GOOGLE_APPLICATION_CREDENTIALS"):
                provider.get_cost_info()

    async def test_send_message_requires_project_and_region(self, tmp_path: Path) -> None:
        provider = self._provider(tmp_path)
        bad = self._valid(tmp_path)
        bad.provider.cloud_ml_region = None
        with patch("openscientist.providers.vertex.get_settings", return_value=bad):
            with pytest.raises(ValueError, match="project_id and region"):
                await provider.send_message([{"role": "user", "content": "hi"}])

    async def test_send_message_with_tools_requires_project_and_region(
        self, tmp_path: Path
    ) -> None:
        provider = self._provider(tmp_path)
        bad = self._valid(tmp_path)
        bad.provider.anthropic_vertex_project_id = None
        with patch("openscientist.providers.vertex.get_settings", return_value=bad):
            with pytest.raises(ValueError, match="project_id and region"):
                await provider.send_message_with_tools(
                    [{"role": "user", "content": "hi"}], tools=[]
                )
