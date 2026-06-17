"""Claude Code agent prompt variants.

The fragments are identity substitutions over the shared bodies, so the
Claude system prompt and ``CLAUDE.md`` are byte-identical to the
pre-split versions.
"""

from openscientist.prompts.common import BackendFragments, build_job_doc

CLAUDE_FRAGMENTS = BackendFragments(
    skills_location="`.claude/skills/`",
    builtin_read_tool="Claude's built-in `Read` tool",
    builtin_read_tool_short="Claude's `Read` tool",
    search_skills_doc=(
        "**search_skills** - Search for domain-specific analysis skills\n"
        "\n"
        "- `query`: Description of the type of analysis needed\n"
        "- `add_to_job=False`: Set True to persist the top result to this job's skill set\n"
        "- Additional skills beyond those in `.claude/skills/` may exist in the database\n"
        "\n"
    ),
    skills_discovery_note=(
        "Use `search_skills` to discover additional skills in the database beyond those pre-loaded."
    ),
)


def generate_job_claude_md(*, use_hypotheses: bool = False, phenix_available: bool = False) -> str:
    """The per-job ``CLAUDE.md`` content for the Claude Code agent."""
    return build_job_doc(
        use_hypotheses=use_hypotheses,
        phenix_available=phenix_available,
        frags=CLAUDE_FRAGMENTS,
    )
