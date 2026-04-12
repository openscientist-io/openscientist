"""Expert subagent loader.

Reads enabled Expert rows and produces the ``dict[str, AgentDefinition]``
consumed by ``ClaudeAgentOptions.agents``.  DB NULL model → ``"inherit"``.
Per-row validation errors are logged and skipped (defense in depth).
"""

from __future__ import annotations

import logging
from typing import Final, Literal, cast, get_args

from claude_agent_sdk.types import AgentDefinition
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from openscientist.database.models import Expert

logger = logging.getLogger(__name__)

ExpertModel = Literal["sonnet", "opus", "haiku", "inherit"]
_VALID_MODELS: Final[frozenset[str]] = frozenset(get_args(ExpertModel))


def _row_to_agent_definition(row: Expert) -> AgentDefinition:
    """Map one Expert row to an AgentDefinition.  Raises ValueError on bad data."""
    model = row.model if row.model is not None else "inherit"
    if model not in _VALID_MODELS:
        raise ValueError(
            f"invalid model {row.model!r}; must be one of {sorted(_VALID_MODELS)} or NULL"
        )
    if row.tools is not None and not isinstance(row.tools, list):
        raise ValueError(f"tools column must be a JSON array, got {type(row.tools).__name__}")
    if row.tools is not None and not all(isinstance(t, str) for t in row.tools):
        raise ValueError("tools array must contain only strings")
    return AgentDefinition(
        description=row.description,
        prompt=row.prompt,
        tools=list(row.tools) if row.tools is not None else None,
        # cast: _VALID_MODELS membership guarantees the Literal constraint.
        model=cast(ExpertModel, model),
    )


async def load_enabled_experts(session: AsyncSession) -> dict[str, AgentDefinition]:
    """Load all enabled experts as an SDK-ready ``agents`` dict."""
    # ORDER BY slug keeps rendered prompt sections deterministic across runs.
    stmt = select(Expert).where(Expert.is_enabled.is_(True)).order_by(Expert.slug)
    result = await session.execute(stmt)

    out: dict[str, AgentDefinition] = {}
    for row in result.scalars():
        try:
            out[row.slug] = _row_to_agent_definition(row)
        except (ValueError, TypeError) as exc:
            logger.warning("Skipping malformed expert row %r: %s", row.slug, exc)
    return out
