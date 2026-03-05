"""Phenix environment setup for structural biology tools."""

import os
import subprocess
import sys
from typing import Any

from openscientist.settings import get_settings


class PhenixConfigError(ValueError):
    """Raised when Phenix configuration is invalid."""


def validate_phenix_path(phenix_path: str) -> list[str]:
    """
    Validate PHENIX_PATH configuration.

    Prefer using `get_settings().phenix` in code paths that already use
    centralized settings validation at startup.

    Args:
        phenix_path: The path to validate.

    Returns:
        List of error messages (empty if valid).
    """
    errors = []

    # Must be an absolute path
    if not phenix_path.startswith("/"):
        errors.append(
            f"PHENIX_PATH must be an absolute path (starting with '/'), "
            f"got: '{phenix_path}'\n"
            f"  Example: PHENIX_PATH=/opt/phenix-1.21.2-5419"
        )
        return errors  # Don't check further if not absolute

    # Must not contain path traversal
    if ".." in phenix_path:
        errors.append(f"PHENIX_PATH must not contain path traversal (..): '{phenix_path}'")

    # Must exist
    if not os.path.exists(phenix_path):
        errors.append(
            f"PHENIX_PATH directory does not exist: '{phenix_path}'\n"
            f"  Please verify the path is correct and the directory exists.\n"
            f"  If using Docker, ensure PHENIX_PATH in .env points to your host installation."
        )

    # Must be a directory
    elif not os.path.isdir(phenix_path):
        errors.append(f"PHENIX_PATH must be a directory, not a file: '{phenix_path}'")

    return errors


def setup_phenix_env(*, raise_on_error: bool = False) -> dict[str, Any] | None:
    """
    Source Phenix environment and return updated environment dict.

    Args:
        raise_on_error: If True, raise PhenixConfigError on invalid config.
                       If False (default), print warnings and return None.

    Returns:
        dict: Environment variables with Phenix paths, or None if not configured/invalid.

    Raises:
        PhenixConfigError: If raise_on_error=True and configuration is invalid.
    """
    phenix_path = get_settings().phenix.phenix_path
    if not phenix_path:
        return None

    # Validate the path
    errors = validate_phenix_path(phenix_path)
    if errors:
        error_msg = "PHENIX_PATH configuration error:\n" + "\n".join(f"  - {e}" for e in errors)
        if raise_on_error:
            raise PhenixConfigError(error_msg)
        print(f"Warning: {error_msg}", file=sys.stderr)
        return None

    env_script = os.path.join(phenix_path, "phenix_env.sh")
    if not os.path.exists(env_script):
        msg = (
            f"Phenix environment script not found at: {env_script}\n"
            f"  This suggests PHENIX_PATH is not a valid Phenix installation.\n"
            f"  Expected to find 'phenix_env.sh' in the Phenix root directory."
        )
        if raise_on_error:
            raise PhenixConfigError(msg)
        print(f"Warning: {msg}", file=sys.stderr)
        return None

    # Source the script and capture environment
    cmd = f"source {env_script} && env"
    try:
        proc = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            executable="/bin/bash",
            timeout=10,
            check=False,
        )

        # Parse environment variables
        phenix_env = os.environ.copy()  # env-ok
        for line in proc.stdout.split("\n"):
            if "=" in line:
                key, _, value = line.partition("=")
                phenix_env[key] = value

        return phenix_env

    except subprocess.TimeoutExpired:
        print("Warning: Phenix environment setup timed out")
        return None
    except (OSError, subprocess.SubprocessError) as e:
        print(f"Warning: Failed to setup Phenix environment: {e}")
        return None


def check_phenix_available() -> bool:
    """
    Check if Phenix is properly configured and available.

    Uses centralized settings when available, with a fallback path for
    situations where settings cannot be loaded.

    Returns:
        bool: True if Phenix is available, False otherwise
    """
    try:
        from openscientist.settings import get_settings

        return get_settings().phenix.is_available
    except Exception:
        # Fall back to original behavior if settings can't be loaded
        return setup_phenix_env() is not None
