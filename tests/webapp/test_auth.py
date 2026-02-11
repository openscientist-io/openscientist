"""Tests for authentication utilities."""

from unittest.mock import Mock, patch

from shandy.webapp_components.utils import auth

# Note: TestIsAuthDisabled and TestIsDevMode are omitted because they
# test trivial behavior (returning settings values) and the autouse
# _disable_auth fixture in conftest.py makes them difficult to isolate.


class TestIsDevMode:
    """Tests for is_dev_mode function."""

    def test_returns_false_by_default(self):
        """Test that is_dev_mode returns False by default."""
        with patch("shandy.webapp_components.utils.auth.get_settings") as mock_settings:
            mock_settings.return_value.dev.dev_mode = False
            assert auth.is_dev_mode() is False

    def test_returns_true_when_enabled(self):
        """Test that is_dev_mode returns True when configured."""
        with patch("shandy.webapp_components.utils.auth.get_settings") as mock_settings:
            mock_settings.return_value.dev.dev_mode = True
            assert auth.is_dev_mode() is True


class TestRequireAuth:
    """Tests for require_auth decorator."""

    def test_auth_disabled_allows_access(self):
        """Test that disabled auth allows access without checking."""
        mock_func = Mock(return_value="result")

        with patch.object(auth, "is_auth_disabled", return_value=True):
            decorated = auth.require_auth(mock_func)
            result = decorated()

        assert result == "result"
        mock_func.assert_called_once()

    def test_authenticated_user_allowed(self):
        """Test that authenticated user can access protected page."""
        mock_func = Mock(return_value="result")
        mock_app = Mock()
        mock_app.storage.user.get.return_value = True

        with patch.object(auth, "is_auth_disabled", return_value=False):
            with patch("shandy.webapp_components.utils.auth.app", mock_app):
                decorated = auth.require_auth(mock_func)
                result = decorated()

        assert result == "result"
        mock_func.assert_called_once()
        mock_app.storage.user.get.assert_called_once_with("authenticated", False)

    def test_unauthenticated_user_redirected(self):
        """Test that unauthenticated user is redirected to login."""
        mock_func = Mock(return_value="result")
        mock_app = Mock()
        mock_app.storage.user.get.return_value = False
        mock_ui = Mock()

        with patch.object(auth, "is_auth_disabled", return_value=False):
            with patch("shandy.webapp_components.utils.auth.app", mock_app):
                with patch("shandy.webapp_components.utils.auth.ui", mock_ui):
                    decorated = auth.require_auth(mock_func)
                    result = decorated()

        # Function should not be called
        mock_func.assert_not_called()
        # Should navigate to login
        mock_ui.navigate.to.assert_called_once_with("/login")
        # Should return None since navigation happened
        assert result is None

    def test_decorator_preserves_function_metadata(self):
        """Test that decorator preserves function name and docstring."""

        def sample_func():
            """Sample docstring."""
            pass

        decorated = auth.require_auth(sample_func)

        assert decorated.__name__ == "sample_func"
        assert decorated.__doc__ == "Sample docstring."

    def test_decorator_with_arguments(self):
        """Test that decorator works with function arguments."""
        mock_func = Mock(return_value="result")
        mock_app = Mock()
        mock_app.storage.user.get.return_value = True

        with patch.object(auth, "is_auth_disabled", return_value=False):
            with patch("shandy.webapp_components.utils.auth.app", mock_app):
                decorated = auth.require_auth(mock_func)
                result = decorated("arg1", kwarg="value")

        assert result == "result"
        mock_func.assert_called_once_with("arg1", kwarg="value")
