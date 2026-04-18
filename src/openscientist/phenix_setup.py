"""Phenix environment setup for structural biology tools."""

import os
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

    if not phenix_path.startswith("/"):
        errors.append(
            f"PHENIX_PATH must be an absolute path (starting with '/'), "
            f"got: '{phenix_path}'\n"
            f"  Example: PHENIX_PATH=/opt/phenix-2.0-5936"
        )
        return errors

    if ".." in phenix_path:
        errors.append(f"PHENIX_PATH must not contain path traversal (..): '{phenix_path}'")

    if not os.path.exists(phenix_path):
        errors.append(
            f"PHENIX_PATH directory does not exist: '{phenix_path}'\n"
            f"  Please verify the path is correct and the directory exists.\n"
            f"  If using Docker, ensure PHENIX_PATH in .env points to your host installation."
        )
    elif not os.path.isdir(phenix_path):
        errors.append(f"PHENIX_PATH must be a directory, not a file: '{phenix_path}'")

    return errors


def setup_phenix_env(*, raise_on_error: bool = False) -> dict[str, Any] | None:
    """
    Build an environment dict for invoking Phenix 2.x tools.

    Phenix 2.x dispatchers in ``bin/`` are self-relocating via
    ``shellrealpath $0``, so we only need to put ``bin/`` on PATH and set
    ``PHENIX`` / ``PHENIX_PREFIX`` (required by tools like ``phenix.list``).

    Args:
        raise_on_error: If True, raise PhenixConfigError on invalid config.
                       If False (default), print warnings and return None.

    Returns:
        dict: Environment variables with Phenix paths, or None if not
        configured/invalid.

    Raises:
        PhenixConfigError: If raise_on_error=True and configuration is invalid.
    """
    phenix_path = get_settings().phenix.phenix_path
    if not phenix_path:
        return None

    errors = validate_phenix_path(phenix_path)
    if errors:
        error_msg = "PHENIX_PATH configuration error:\n" + "\n".join(f"  - {e}" for e in errors)
        if raise_on_error:
            raise PhenixConfigError(error_msg)
        print(f"Warning: {error_msg}", file=sys.stderr)
        return None

    bin_dir = os.path.join(phenix_path, "bin")
    if not os.path.isdir(bin_dir) or not os.path.exists(os.path.join(bin_dir, "phenix.about")):
        msg = (
            f"Phenix installation at {phenix_path} is missing bin/phenix.about. "
            f"Expected a Phenix 2.x install layout."
        )
        if raise_on_error:
            raise PhenixConfigError(msg)
        print(f"Warning: {msg}", file=sys.stderr)
        return None

    phenix_env = os.environ.copy()  # env-ok
    phenix_env["PATH"] = f"{bin_dir}:{phenix_env.get('PATH', '')}"
    phenix_env["PHENIX"] = phenix_path
    phenix_env["PHENIX_PREFIX"] = phenix_path
    return phenix_env


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
        return setup_phenix_env() is not None
