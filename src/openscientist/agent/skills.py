"""Per-job skill-file writers, shared by the concrete agents.

Each backend materialises the enabled skills in its own on-disk layout from
its ``prepare_job_workspace``: Claude writes ``.claude/CLAUDE.md`` plus
``.claude/skills/*.md``. Codex writes native ``.agents/skills/*/SKILL.md``
files it auto-discovers. These live in the agent layer (not the orchestrator)
so the agents do not depend back on ``orchestrator.discovery``.
"""

from __future__ import annotations

import logging
from pathlib import Path

from openscientist.database.models import Skill
from openscientist.database.session import AsyncSessionLocal
from openscientist.prompts import generate_job_claude_md, get_enabled_skills
from openscientist.settings import get_settings

logger = logging.getLogger(__name__)


def _write_job_claude_md(claude_dir: Path, *, use_hypotheses: bool = False) -> None:
    """Write the generated discovery JOB CLAUDE.md into ``claude_dir``."""
    try:
        phenix_available = get_settings().phenix.is_available
        dest = claude_dir / "CLAUDE.md"
        dest.write_text(
            generate_job_claude_md(
                use_hypotheses=use_hypotheses, phenix_available=phenix_available
            ),
            encoding="utf-8",
        )
        logger.debug("Wrote job CLAUDE.md to %s (use_hypotheses=%s)", dest, use_hypotheses)
    except Exception as e:
        logger.warning("Failed to write job CLAUDE.md: %s", e)


async def write_skills_to_claude_dir(job_dir: Path, *, use_hypotheses: bool = False) -> None:
    """Write CLAUDE.md and enabled skill files into ``job_dir/.claude/``."""
    claude_dir = job_dir / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Write the discovery-agent JOB CLAUDE.md (hypothesis sections conditional)
    _write_job_claude_md(claude_dir, use_hypotheses=use_hypotheses)

    try:
        async with AsyncSessionLocal(thread_safe=True) as session:
            skills = await get_enabled_skills(session)
        if not skills:
            logger.info("No enabled skills to write")
            return
        skills_dir = claude_dir / "skills"
        skills_dir.mkdir(parents=True, exist_ok=True)
        for skill in skills:
            filename = f"{skill.category}--{skill.slug}.md"
            path = skills_dir / filename
            header = f"# {skill.name}\n*Category: {skill.category}*\n"
            if skill.description:
                header += f"\n{skill.description}\n"
            path.write_text(header + "\n" + skill.content, encoding="utf-8")
        logger.info("Wrote %d skill files to %s", len(skills), skills_dir)
    except Exception as e:
        logger.warning("Failed to write skills to .claude dir: %s", e)


def _yaml_quote(value: str) -> str:
    """Render a YAML double-quoted scalar so colons and other special
    characters cannot break SKILL.md frontmatter parsing."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def codex_skill_markdown(skill: Skill) -> str:
    """Render one enabled skill as a codex ``SKILL.md`` (frontmatter + body).

    Codex caps the frontmatter ``name`` at 64 chars and DROPS any skill whose
    ``description`` is empty, so the name is truncated and the description is
    collapsed to a single line, bounded to 1024 chars, with a non-empty
    fallback.
    """
    name = f"{skill.category}--{skill.slug}"[:64]
    description = " ".join((skill.description or "").split())[:1024]
    if not description:
        description = f"{skill.category} skill: {skill.name}"
    frontmatter = f"---\nname: {_yaml_quote(name)}\ndescription: {_yaml_quote(description)}\n---\n"
    return frontmatter + skill.content


async def write_skills_to_codex_dir(job_dir: Path) -> None:
    """Write enabled skills as codex ``SKILL.md`` files into
    ``job_dir/.agents/skills/``.

    The codex agent runs with the job dir as its cwd (a git repo), so codex
    treats ``.agents/skills/`` as a project skill root: it discovers each
    ``SKILL.md`` and auto-injects a ``## Skills`` summary into the system
    prompt with its own trigger rules. This is how the codex/Ollama agent
    receives skills; the ``.claude/`` path does not apply to it.
    """
    try:
        async with AsyncSessionLocal(thread_safe=True) as session:
            skills = await get_enabled_skills(session)
        if not skills:
            logger.info("No enabled skills to write")
            return
        skills_root = job_dir / ".agents" / "skills"
        for skill in skills:
            skill_dir = skills_root / f"{skill.category}--{skill.slug}"
            skill_dir.mkdir(parents=True, exist_ok=True)
            (skill_dir / "SKILL.md").write_text(codex_skill_markdown(skill), encoding="utf-8")
        logger.info("Wrote %d codex skill files to %s", len(skills), skills_root)
    except Exception as e:
        logger.warning("Failed to write skills to .agents dir: %s", e)
