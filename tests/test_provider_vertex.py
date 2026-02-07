"""Tests for Vertex AI provider."""

import os
from unittest.mock import patch

import pytest

from shandy.providers.vertex import VertexProvider


class TestVertexProviderValidation:
    """Tests for Vertex AI provider configuration validation."""

    def test_valid_config(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "012345-ABCDEF",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            provider = VertexProvider()
            assert "vertex" in provider.name.lower()

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_all_config_raises(self):
        with pytest.raises(ValueError, match="configuration errors"):
            VertexProvider()

    def test_missing_creds_file_raises(self, tmp_path):
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_VERTEX_PROJECT_ID": "proj",
                "GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/creds.json",
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            with pytest.raises(ValueError, match="not found"):
                VertexProvider()


class TestVertexSetupEnvironment:
    """Tests for Vertex AI environment setup."""

    def test_setup_does_not_raise(self, tmp_path):
        creds = tmp_path / "creds.json"
        creds.write_text('{"type": "service_account"}')
        with patch.dict(
            os.environ,
            {
                "ANTHROPIC_VERTEX_PROJECT_ID": "my-project",
                "GOOGLE_APPLICATION_CREDENTIALS": str(creds),
                "GCP_BILLING_ACCOUNT_ID": "id",
                "CLOUD_ML_REGION": "us-east5",
            },
        ):
            provider = VertexProvider()
            provider.setup_environment()  # should just log, not raise
