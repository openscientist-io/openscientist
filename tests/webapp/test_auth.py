"""Tests for authentication utilities."""

import os
from unittest.mock import Mock, patch

import bcrypt

from shandy.webapp_components.utils import auth


class TestCheckPassword:
    """Tests for check_password function."""

    def test_no_password_hash_set(self):
        """Test that no password hash allows access."""
        with patch.object(auth, "PASSWORD_HASH", b""):
            assert auth.check_password("any_password") is True

    def test_correct_password(self):
        """Test correct password validation."""
        test_password = "test123"
        password_hash = bcrypt.hashpw(test_password.encode(), bcrypt.gensalt())

        with patch.object(auth, "PASSWORD_HASH", password_hash):
            assert auth.check_password(test_password) is True

    def test_incorrect_password(self):
        """Test incorrect password rejection."""
        test_password = "test123"
        wrong_password = "wrong456"
        password_hash = bcrypt.hashpw(test_password.encode(), bcrypt.gensalt())

        with patch.object(auth, "PASSWORD_HASH", password_hash):
            assert auth.check_password(wrong_password) is False

    def test_password_check_exception_handling(self):
        """Test that exceptions in password checking return False."""
        with patch.object(auth, "PASSWORD_HASH", b"invalid_hash"):
            # bcrypt.checkpw will raise an exception with invalid hash
            assert auth.check_password("any_password") is False


class TestRequireAuth:
    """Tests for require_auth decorator."""

    def test_auth_disabled_allows_access(self):
        """Test that disabled auth allows access without checking."""
        mock_func = Mock(return_value="result")

        with patch.object(auth, "DISABLE_AUTH", True):
            decorated = auth.require_auth(mock_func)
            result = decorated()

        assert result == "result"
        mock_func.assert_called_once()

    def test_authenticated_user_allowed(self):
        """Test that authenticated user can access protected page."""
        mock_func = Mock(return_value="result")
        mock_app = Mock()
        mock_app.storage.user.get.return_value = True

        with patch.object(auth, "DISABLE_AUTH", False):
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

        with patch.object(auth, "DISABLE_AUTH", False):
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

        with patch.object(auth, "DISABLE_AUTH", False):
            with patch("shandy.webapp_components.utils.auth.app", mock_app):
                decorated = auth.require_auth(mock_func)
                result = decorated("arg1", kwarg="value")

        assert result == "result"
        mock_func.assert_called_once_with("arg1", kwarg="value")


class TestAuthConfiguration:
    """Tests for authentication configuration."""

    def test_disable_auth_environment_variable(self):
        """Test that DISABLE_AUTH reads from environment properly."""
        # This test verifies the module reads the env var correctly
        # In actual usage, this is set at module import time
        with patch.dict(os.environ, {"DISABLE_AUTH": "true"}):
            # Reimport to pick up env var
            import importlib

            importlib.reload(auth)
            assert auth.DISABLE_AUTH is True

        with patch.dict(os.environ, {"DISABLE_AUTH": "false"}):
            importlib.reload(auth)
            assert auth.DISABLE_AUTH is False

        # Restore original state
        importlib.reload(auth)
