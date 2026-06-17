"""Prompt templates for the OpenScientist orchestrator.

The shared, backend-agnostic prompt bodies live in `common`, parameterised
over backend fragments. Each agent owns its fragments and builds its own
system prompt, job doc, and chat context via the `AbstractAgent` contract.
The per-job doc generators and concrete fragments live in `claude` / `codex`.
"""

from openscientist.prompts.claude import generate_job_claude_md
from openscientist.prompts.common import (
    build_discovery_prompt,
    format_skills_list,
    get_enabled_skills,
)

__all__ = [
    "build_discovery_prompt",
    "format_skills_list",
    "generate_job_claude_md",
    "get_enabled_skills",
]
