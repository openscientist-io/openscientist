"""Tests for authentication utilities."""

from unittest.mock import patch

from openscientist.webapp_components.utils import auth

# Note: TestIsDevMode is minimal because it tests trivial behavior
# (returning a settings value).


class TestIsDevMode:
    """Tests for is_dev_mode function."""

    def test_returns_false_by_default(self):
        """Test that is_dev_mode returns False by default."""
        with patch("openscientist.webapp_components.utils.auth.get_settings") as mock_settings:
            mock_settings.return_value.dev.dev_mode = False
            assert auth.is_dev_mode() is False

    def test_returns_true_when_enabled(self):
        """Test that is_dev_mode returns True when configured."""
        with patch("openscientist.webapp_components.utils.auth.get_settings") as mock_settings:
            mock_settings.return_value.dev.dev_mode = True
            assert auth.is_dev_mode() is True
