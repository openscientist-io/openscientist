"""Shared environment cleanup helpers for provider setup."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable

CLAUDE_PROVIDER_MODE_FLAGS = (
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_FOUNDRY",
)

VERTEX_PROVIDER_ENV_VARS = (
    "ANTHROPIC_VERTEX_PROJECT_ID",
    "VERTEX_REGION_CLAUDE_4_5_SONNET",
    "VERTEX_REGION_CLAUDE_4_5_HAIKU",
)


def clear_provider_mode_flags(
    logger: logging.Logger,
    *,
    active_flag: str | None = None,
) -> None:
    """Clear all provider mode flags except the optional active one."""
    for var in CLAUDE_PROVIDER_MODE_FLAGS:
        if var == active_flag:
            continue
        if os.environ.pop(var, None) is not None:  # env-ok
            logger.debug("Removing conflicting %s", var)


def clear_env_vars(logger: logging.Logger, vars_to_clear: Iterable[str]) -> None:
    """Unset each environment variable if present."""
    for var in vars_to_clear:
        if os.environ.pop(var, None) is not None:  # env-ok
            logger.debug("Removing conflicting %s", var)


def clear_empty_env_vars(logger: logging.Logger, vars_to_clear: Iterable[str]) -> None:
    """Unset variables that are set to an empty string."""
    for var in vars_to_clear:
        if os.environ.get(var) == "":  # env-ok
            os.environ.pop(var, None)  # env-ok
            logger.debug("Unset empty %s", var)
