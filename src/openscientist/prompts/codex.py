"""Codex agent prompt variants.

The Codex agent reads its instructions from ``AGENTS.md`` and has no
``.claude/`` directory, so the fragments drop Claude-specific paths and
the ``Read`` tool name. Skills are delivered as native codex ``SKILL.md``
files under ``.agents/skills/`` (see ``agent.skills.write_skills_to_codex_dir``),
which codex auto-injects as a ``## Skills`` section, so the prompt points at
that section and drops the nonexistent ``search_skills`` tool.
"""

from openscientist.prompts.common import BackendFragments

CODEX_FRAGMENTS = BackendFragments(
    skills_location=(
        "the `## Skills` section of this prompt (codex lists each available "
        "skill and the path to its `SKILL.md` there)"
    ),
    builtin_read_tool="the built-in file-reading tool",
    builtin_read_tool_short="the built-in file-reading tool",
    search_skills_doc="",
    skills_discovery_note="",
)
