"""Prompt templates for the OpenScientist orchestrator.

The shared bodies live in ``common``, parameterised over backend fragments,
and each agent owns its fragments.
"""

from openscientist.prompts.claude import generate_job_claude_md
from openscientist.prompts.common import get_enabled_skills

__all__ = [
    "generate_job_claude_md",
    "get_enabled_skills",
]
