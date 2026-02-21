"""Tests for Vertex AI provider."""

import os
from unittest.mock import patch

import pytest

from shandy.providers.vertex import VertexProvider
from shandy.settings import clear_settings_cache


class TestVertexProviderValidation:
    """Tests for Vertex AI provider configuration validation."""

    def test_valid_config(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "vertex",
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            clear_settings_cache()
            provider = VertexProvider()
            assert "vertex" in provider.name.lower()

    def test_missing_creds_file_raises(self, tmp_path):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "vertex",
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
        mock_settings.provider.anthropic_model = "model"
        mock_settings.provider.vertex_region_claude_4_5_sonnet = None
        mock_settings.provider.vertex_region_claude_4_5_haiku = None

        with (
            patch("shandy.providers.vertex.get_settings", return_value=mock_settings),
            patch("os.path.exists", return_value=True),
        ):
            with pytest.raises(ValueError, match="ANTHROPIC_VERTEX_PROJECT_ID"):
                VertexProvider()

    def test_missing_credentials_raises(self):
        from unittest.mock import MagicMock

        mock_settings = MagicMock()
        mock_settings.provider.anthropic_vertex_project_id = "proj"
        mock_settings.provider.google_application_credentials = None
        mock_settings.provider.gcp_billing_account_id = "id"
        mock_settings.provider.cloud_ml_region = "us-east5"
        mock_settings.provider.anthropic_model = "model"
        mock_settings.provider.vertex_region_claude_4_5_sonnet = None
        mock_settings.provider.vertex_region_claude_4_5_haiku = None

        with patch("shandy.providers.vertex.get_settings", return_value=mock_settings):
            with pytest.raises(ValueError, match="GOOGLE_APPLICATION_CREDENTIALS"):
                VertexProvider()

    def test_credentials_file_not_found_raises(self, tmp_path):
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "vertex",
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
        mock_settings.provider.anthropic_model = None
        mock_settings.provider.vertex_region_claude_4_5_sonnet = None
        mock_settings.provider.vertex_region_claude_4_5_haiku = None

        with (
            patch("shandy.providers.vertex.get_settings", return_value=mock_settings),
            patch("os.path.exists", return_value=True),
        ):
            # Should not raise — optional warnings don't prevent init
            provider = VertexProvider()
            assert provider.name == "Vertex AI"


class TestVertexSetupEnvironment:
    """Tests for Vertex AI environment setup."""

    def test_setup_does_not_raise(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "CLAUDE_PROVIDER": "vertex",
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
                "CLAUDE_PROVIDER": "vertex",
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
