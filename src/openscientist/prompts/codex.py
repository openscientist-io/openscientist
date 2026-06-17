"""Codex agent prompt variants.

The Codex agent reads its instructions from ``AGENTS.md`` and has no
``.claude/`` directory, so the fragments drop Claude-specific paths and the
``Read`` tool name. Skills are written as native codex ``SKILL.md`` files
under ``.agents/skills/<category>--<slug>/`` (see
``agent.skills.write_skills_to_codex_dir``). The agent runs with the job dir
as cwd, so the prompt points at the literal directory path and the agent
discovers + reads SKILL.md files with its built-in file tools. The
``search_skills`` tool is Claude-only and is dropped.
"""

from openscientist.prompts.common import BackendFragments

CODEX_FRAGMENTS = BackendFragments(
    skills_location="`.agents/skills/`",
    builtin_read_tool="the built-in file-reading tool",
    builtin_read_tool_short="the built-in file-reading tool",
    search_skills_doc="",
    skills_discovery_note=(
        "Each skill is a subdirectory `<category>--<slug>/` containing a "
        "`SKILL.md` file. Read them with shell commands "
        "(e.g. `cat .agents/skills/workflow--hypothesis-generation/SKILL.md`), "
        "NOT with `execute_code` (Python): the Python executor runs in a "
        "separate sandbox that only sees `/data` and `/output` and cannot "
        "access `.agents/skills/`."
    ),
)
