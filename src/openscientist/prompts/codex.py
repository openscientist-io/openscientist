"""Codex agent prompt variants.

Codex has no ``.claude/`` dir, so the fragments drop the Claude paths and the
``Read`` tool name. Skills are auto-injected as a ``## Skills`` section, so the
prompt points there.
"""

from openscientist.prompts.common import BackendFragments

CODEX_FRAGMENTS = BackendFragments(
    skills_location=(
        "the `## Skills` section of this prompt (codex lists each available "
        "skill and the path to its `SKILL.md` there)"
    ),
    builtin_read_tool="the built-in file-reading tool",
    builtin_read_tool_short="the built-in file-reading tool",
    # Codex exposes the MCP tools under their bare names.
    mcp_tool_prefix="",
)
