"""
Version information for SHANDY.

Provides build metadata from Docker build args or git.
"""

import os
import subprocess

# Version constants
__version__ = "0.1.0"

# Build metadata (set during Docker build via environment variables)
_commit: str | None = None
_build_time: str | None = None


def get_commit() -> str:
    """Get the git commit hash."""
    global _commit
    if _commit is not None:
        return _commit

    # Try environment variable first (set during Docker build)
    commit = os.environ.get("SHANDY_COMMIT", "")  # env-ok
    if commit and commit != "unknown":
        _commit = commit[:12]
        return _commit

    # Fallback to git
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            _commit = result.stdout.strip()[:12]
            return _commit
    except (OSError, subprocess.SubprocessError):
        pass

    _commit = "unknown"
    return _commit


def get_build_time() -> str:
    """Get the build timestamp."""
    global _build_time
    if _build_time is not None:
        return _build_time

    build_time = os.environ.get("SHANDY_BUILD_TIME", "")  # env-ok
    if build_time and build_time != "unknown":
        _build_time = build_time
        return _build_time

    _build_time = "dev"
    return _build_time


def get_version_string() -> str:
    """Get a full version string with commit and build time."""
    return f"SHANDY v{__version__} (commit: {get_commit()}, built: {get_build_time()})"
