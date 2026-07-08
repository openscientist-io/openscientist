"""Per-job state for the standalone tools MCP server.

State is bound from ``OPENSCIENTIST_*`` environment variables at
module import. Missing required vars crash the server at start-up
via a Pydantic ``ValidationError`` that names the missing field.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class ToolServerState(BaseSettings):
    """Env-var-bound state mirroring the in-process ``ToolContext``."""

    model_config = SettingsConfigDict(
        env_prefix="OPENSCIENTIST_",
        case_sensitive=False,
        extra="ignore",
    )

    job_id: str
    job_dir: Path
    data_file: Path | None = None
    # NoDecode suppresses pydantic-settings' default JSON-decode for complex
    # types so the env value reaches `_split_pathsep` as a raw string and gets
    # split on `os.pathsep`.
    data_files: Annotated[tuple[Path, ...], NoDecode] = ()
    use_hypotheses: bool = False
    marduk_enabled: bool = False

    @field_validator("data_files", mode="before")
    @classmethod
    def _split_pathsep(cls, value: object) -> object:
        if isinstance(value, str):
            return tuple(Path(p) for p in value.split(os.pathsep) if p)
        return value


STATE = ToolServerState()
