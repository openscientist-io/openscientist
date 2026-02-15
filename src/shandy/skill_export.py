"""
Skill export utilities for container mounting.

Exports all enabled skills from the database to a directory structure
that can be mounted into agent containers.
"""

import logging
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Skill

logger = logging.getLogger(__name__)


async def export_skills_to_directory(
    session: AsyncSession,
    target_dir: Path,
) -> int:
    """
    Export all enabled skills to a directory for container mounting.

    Creates a directory structure like:
        target_dir/
        ├── workflow/
        │   └── hypothesis-generation/
        │       └── SKILL.md
        ├── domain/
        │   └── metabolomics/
        │       └── SKILL.md
        ...

    Each skill is exported as SKILL.md in its category/slug directory.

    Args:
        session: Database session
        target_dir: Directory to export skills to

    Returns:
        Number of skills exported
    """
    # Get all enabled skills
    stmt = (
        select(Skill)
        .where(Skill.is_enabled == True)  # noqa: E712
        .order_by(Skill.category, Skill.name)
    )
    result = await session.execute(stmt)
    skills = list(result.scalars().all())

    if not skills:
        logger.info("No enabled skills to export")
        return 0

    # Create target directory
    target_dir.mkdir(parents=True, exist_ok=True)

    exported = 0
    for skill in skills:
        # Create category/slug directory
        skill_dir = target_dir / skill.category / skill.slug
        skill_dir.mkdir(parents=True, exist_ok=True)

        # Write skill content as SKILL.md
        skill_path = skill_dir / "SKILL.md"

        # Build skill content with frontmatter
        content_parts = [
            "---",
            f"name: {skill.name}",
            f"category: {skill.category}",
            f"slug: {skill.slug}",
        ]

        if skill.description:
            # Escape description for YAML
            desc = skill.description.replace('"', '\\"')
            content_parts.append(f'description: "{desc}"')

        if skill.tags:
            tags_str = ", ".join(skill.tags)
            content_parts.append(f"tags: [{tags_str}]")

        content_parts.append("---")
        content_parts.append("")
        content_parts.append(skill.content)

        skill_content = "\n".join(content_parts)

        skill_path.write_text(skill_content, encoding="utf-8")
        exported += 1

        logger.debug("Exported skill: %s/%s", skill.category, skill.slug)

    logger.info("Exported %d skills to %s", exported, target_dir)
    return exported


async def export_skills_summary(
    session: AsyncSession,
) -> str:
    """
    Generate a summary of all enabled skills for the agent prompt.

    Returns a markdown-formatted list of available skills.

    Args:
        session: Database session

    Returns:
        Formatted skills summary string
    """
    stmt = (
        select(Skill)
        .where(Skill.is_enabled == True)  # noqa: E712
        .order_by(Skill.category, Skill.name)
    )
    result = await session.execute(stmt)
    skills = list(result.scalars().all())

    if not skills:
        return ""

    # Group by category
    categories: dict[str, list[Skill]] = {}
    for skill in skills:
        if skill.category not in categories:
            categories[skill.category] = []
        categories[skill.category].append(skill)

    # Build summary
    lines = ["## Available Skills", ""]
    for category, category_skills in sorted(categories.items()):
        lines.append(f"### {category.title()}")
        for skill in category_skills:
            desc = skill.description or "No description"
            lines.append(f"- **{skill.name}** (`{category}/{skill.slug}`): {desc}")
        lines.append("")

    return "\n".join(lines)


def get_skills_directory() -> Path:
    """
    Get the standard skills export directory.

    Returns:
        Path to the skills export directory
    """
    return Path("agent/.claude/skills")


async def ensure_skills_exported(session: AsyncSession) -> Path:
    """
    Ensure skills are exported to the standard directory.

    This is a convenience function that exports skills if the directory
    is empty or doesn't exist.

    Args:
        session: Database session

    Returns:
        Path to the skills directory
    """
    skills_dir = get_skills_directory()

    # Check if any skills are exported
    existing_skills = list(skills_dir.glob("**/SKILL.md")) if skills_dir.exists() else []

    if not existing_skills:
        await export_skills_to_directory(session, skills_dir)

    return skills_dir
