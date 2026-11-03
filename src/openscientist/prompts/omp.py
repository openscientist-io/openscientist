"""Oh My Pi (omp) prompt fragments.

Like the Codex fragments: omp surfaces skills natively (from ``.omp/skills/``),
so these drop the ``.claude/`` paths and use omp's built-in ``read``/``write``
tools. The MCP tools are namespaced as ``mcp__<server>_<tool>``, which
``mcp_tool_prefix`` renames the shared prompt's mentions to.
"""

from openscientist.prompts.common import BackendFragments

OMP_FRAGMENTS = BackendFragments(
    skills_location=(
        "the skills omp lists in this prompt (omp surfaces each available "
        "skill and the path to its `SKILL.md` natively)"
    ),
    builtin_read_tool="the built-in `read` tool",
    builtin_read_tool_short="the built-in `read` tool",
    # omp exposes MCP tools top-level (OmpAgent disables tools.xdev) but keeps
    # them namespaced by server, so the prompts must name them that way.
    mcp_tool_prefix="mcp__openscientist_tools_",
)
