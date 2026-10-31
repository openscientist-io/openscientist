"""Claude Code agent prompt variants.

The shared bodies are written in Claude's vocabulary, so these fragments
substitute each phrase with itself.
"""

from openscientist.prompts.common import BackendFragments, build_job_doc

CLAUDE_FRAGMENTS = BackendFragments(
    skills_location="`.claude/skills/`",
    builtin_read_tool="Claude's built-in `Read` tool",
    builtin_read_tool_short="Claude's `Read` tool",
    # Claude exposes the MCP tools under their bare names.
    mcp_tool_prefix="",
)


def generate_job_claude_md(*, use_hypotheses: bool = False, phenix_available: bool = False) -> str:
    """The per-job ``CLAUDE.md`` content for the Claude Code agent."""
    return build_job_doc(
        use_hypotheses=use_hypotheses,
        phenix_available=phenix_available,
        frags=CLAUDE_FRAGMENTS,
    )
