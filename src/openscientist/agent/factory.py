"""
AgentExecutor factory for OpenScientist.

Always returns an SDKAgentExecutor (backed by claude-agent-sdk).
The provider's setup_environment() configures the correct env vars
so the SDK's bundled CLI routes to the right backend.
"""

from __future__ import annotations

import logging
from pathlib import Path

from claude_agent_sdk.types import AgentDefinition

from openscientist.agent.protocol import AgentExecutor
from openscientist.agent.sdk_executor import SDKAgentExecutor

logger = logging.getLogger(__name__)


def get_agent_executor(
    job_dir: Path,
    data_file: Path | None,
    system_prompt: str | None,
    use_hypotheses: bool = False,
    data_files: list[Path] | None = None,
    experts: dict[str, AgentDefinition] | None = None,
) -> AgentExecutor:
    """
    Return an SDKAgentExecutor for the configured provider.

    Args:
        job_dir: Path to the job directory
        data_file: Optional path to the primary data file
        system_prompt: System prompt to use
        use_hypotheses: Whether to include hypothesis tracking tools
        data_files: All data files for this job (used for multi-file metadata)
        experts: Optional mapping of subagent slug to AgentDefinition,
            forwarded to the SDK's ``agents=`` kwarg at session init.

    Returns:
        An SDKAgentExecutor instance
    """
    logger.info("Using SDKAgentExecutor")
    return SDKAgentExecutor(
        job_dir=job_dir,
        data_file=data_file,
        system_prompt=system_prompt,
        use_hypotheses=use_hypotheses,
        data_files=data_files,
        experts=experts,
    )
