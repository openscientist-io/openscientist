"""
Prompt templates for SHANDY orchestrator.

System prompts and discovery iteration prompts for the autonomous agent.
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .database.models import Skill


async def get_relevant_skills(
    session: AsyncSession,
    research_question: str,
    limit: int = 5,
) -> List[Skill]:
    """
    Get skills relevant to research question using full-text search.

    Uses PostgreSQL's tsvector search to find skills whose content
    matches the research question. Falls back to returning all enabled
    skills if no matches found via search.

    Args:
        session: Database session
        research_question: The research question to match against
        limit: Maximum number of skills to return

    Returns:
        List of relevant Skill objects, ordered by relevance
    """
    # Try full-text search first
    search_query = func.plainto_tsquery("english", research_question)

    stmt = (
        select(Skill)
        .where(Skill.is_enabled == True)  # noqa: E712
        .where(Skill.search_vector.op("@@")(search_query))
        .order_by(func.ts_rank(Skill.search_vector, search_query).desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    skills = list(result.scalars().all())

    # If no matches, fall back to all enabled skills (up to limit)
    if not skills:
        stmt = (
            select(Skill)
            .where(Skill.is_enabled == True)  # noqa: E712
            .order_by(Skill.category, Skill.name)
            .limit(limit)
        )
        result = await session.execute(stmt)
        skills = list(result.scalars().all())

    return skills


async def get_relevant_skills_with_scores(
    session: AsyncSession,
    research_question: str,
    limit: int = 5,
) -> List[tuple[Skill, float]]:
    """
    Get skills with their relevance scores using trigram similarity.

    Uses PostgreSQL's pg_trgm extension for fuzzy matching against
    skill name, description, and content. Returns skills ordered by
    similarity score.

    Args:
        session: Database session
        research_question: The research question to match against
        limit: Maximum number of skills to return

    Returns:
        List of (Skill, score) tuples, ordered by similarity (highest first)
    """
    # Use word_similarity for better matching of phrases against longer text
    # Combine similarities: name (highest weight), description, content
    similarity_score = (
        func.coalesce(func.word_similarity(research_question, Skill.name), 0) * 3
        + func.coalesce(func.word_similarity(research_question, Skill.description), 0) * 2
        + func.coalesce(func.word_similarity(research_question, Skill.content), 0)
    ) / 6  # Normalize to 0-1 range

    stmt = (
        select(Skill, similarity_score.label("score"))
        .where(Skill.is_enabled == True)  # noqa: E712
        .order_by(similarity_score.desc())
        .limit(limit)
    )
    result = await session.execute(stmt)
    skills_with_scores = [(row.Skill, float(row.score)) for row in result]

    return skills_with_scores


def format_skills_content(skills: List[Skill]) -> str:
    """
    Format skills into content suitable for system prompt injection.

    Creates a structured markdown document with skills grouped by category,
    including descriptions and full content.

    Args:
        skills: List of Skill objects to format

    Returns:
        Formatted markdown string for system prompt
    """
    if not skills:
        return ""

    parts = [
        "# Domain-Specific Skills",
        "",
        "The following skills have been matched to your research question.",
        "Use these as guidance for analysis approaches and best practices.",
        "",
    ]

    # Group skills by category
    categories: Dict[str, List[Skill]] = {}
    for skill in skills:
        if skill.category not in categories:
            categories[skill.category] = []
        categories[skill.category].append(skill)

    for category, category_skills in sorted(categories.items()):
        parts.append(f"## {category.title()} Skills")
        parts.append("")

        for skill in category_skills:
            parts.append(f"### {skill.name}")
            if skill.description:
                parts.append(f"*{skill.description}*")
            parts.append("")
            parts.append(skill.content)
            parts.append("")

    return "\n".join(parts)


def get_system_prompt(skills_enabled: bool = True) -> str:
    """
    Get system prompt for Claude.

    Args:
        skills_enabled: Whether skills are enabled for this session

    Returns:
        System prompt string
    """
    if skills_enabled:
        return """You are an autonomous scientific discovery agent. Your goal is to discover mechanistic insights from scientific data through iterative hypothesis testing.

**Your Capabilities:**

You have access to tools:
- `execute_code`: Run Python code to analyze data (pandas, numpy, scipy, matplotlib, seaborn, statsmodels, sklearn, networkx)
- `search_pubmed`: Search scientific literature for relevant papers
- `search_skills`: Search for additional domain-specific skills and guidance (e.g., "metabolomics analysis", "statistical testing")
- `update_knowledge_state`: Record a confirmed finding
- `set_status`: Update your current status message (e.g., "Analyzing correlation between X and Y")
- `set_job_title`: Set a brief title for this job (e.g., "Kinase inhibitor binding analysis")

IMPORTANT:
- Call `set_job_title` early (iteration 1) to give the job a meaningful, concise title
- Call `set_status` at the START of each significant action to let users know what you're working on

You have access to skills that provide structured workflows:
- hypothesis-generation: How to formulate testable hypotheses
- result-interpretation: How to interpret statistical results
- prioritization: How to decide what to investigate next
- stopping-criteria: When to stop investigating

**Your Approach:**

1. **Explore** the data to identify patterns
2. **Generate hypotheses** using literature and domain knowledge (use skills for guidance)
3. **Test hypotheses** by writing Python code for statistical analyses
4. **Interpret results** - both positive AND negative findings are valuable
5. **Iterate** - use findings to generate new hypotheses
6. **Learn from failures** - rejected hypotheses guide future investigation

**Important Principles:**

- Write clear, well-documented Python code
- Always check assumptions (normality, homoscedasticity, etc.)
- Report effect sizes, not just p-values
- Negative results are valuable - they rule out hypotheses
- Search literature proactively to inform hypothesis generation
- Don't repeat failed hypotheses

Think step by step. Be rigorous. Be creative."""

    else:
        # Skills disabled - pure LLM reasoning
        return """You are an autonomous scientific discovery agent. Your goal is to discover mechanistic insights from scientific data through iterative hypothesis testing.

**Your Capabilities:**

You have access to tools:
- `execute_code`: Run Python code to analyze data (pandas, numpy, scipy, matplotlib, seaborn, statsmodels, sklearn, networkx)
- `search_pubmed`: Search scientific literature for relevant papers
- `update_knowledge_state`: Record a confirmed finding
- `set_status`: Update your current status message (e.g., "Analyzing correlation between X and Y")
- `set_job_title`: Set a brief title for this job (e.g., "Kinase inhibitor binding analysis")

IMPORTANT:
- Call `set_job_title` early (iteration 1) to give the job a meaningful, concise title
- Call `set_status` at the START of each significant action to let users know what you're working on

**Your Approach:**

You have maximum freedom to design your own analytical strategy. Think creatively about:
1. What patterns to look for
2. What hypotheses to test
3. What analyses to run
4. How to interpret results
5. When to pivot or dive deeper

**Important Principles:**

- Write clear, well-documented Python code
- Always check assumptions (normality, homoscedasticity, etc.)
- Report effect sizes, not just p-values
- Negative results are valuable - they rule out hypotheses
- Search literature proactively to inform hypothesis generation
- Don't repeat failed hypotheses

Think step by step. Be rigorous. Be creative."""


def build_discovery_prompt(
    knowledge_graph_summary: str,
    iteration: int,
    max_iterations: int,
    skills_available: Optional[str] = None,
) -> str:
    """
    Build the discovery iteration prompt.

    Args:
        knowledge_graph_summary: Formatted KS summary from KnowledgeState.get_summary()
        iteration: Current iteration number
        max_iterations: Maximum iterations allowed
        skills_available: Optional formatted list of available skills

    Returns:
        Prompt string for this iteration
    """
    prompt_parts = [
        f"# Iteration {iteration}/{max_iterations}",
        "",
        knowledge_graph_summary,
        "",
        "---",
        "",
        "## Your Task",
        "",
        f"You are on iteration {iteration} of {max_iterations}.",
        "",
    ]

    # Guidance based on progress
    if iteration == 1:
        prompt_parts.extend(
            [
                "This is your **first iteration**. Start by:",
                "1. Understanding the data structure and available variables",
                "2. Searching literature to understand the research domain",
                "3. Identifying 2-3 high-priority hypotheses to investigate",
                "",
            ]
        )
    elif iteration < 5:
        prompt_parts.extend(
            [
                "You're in the **early exploration phase**. Focus on:",
                "1. Identifying major patterns and group differences",
                "2. Testing broad hypotheses",
                "3. Building intuition about the data",
                "",
            ]
        )
    elif iteration < max_iterations - 10:
        prompt_parts.extend(
            [
                "You're in the **deep investigation phase**. Focus on:",
                "1. Following up on interesting findings",
                "2. Testing mechanistic hypotheses",
                "3. Connecting findings into a coherent story",
                "",
            ]
        )
    else:
        prompt_parts.extend(
            [
                "You're **approaching the iteration limit**. Focus on:",
                "1. Consolidating findings",
                "2. Testing remaining high-priority hypotheses",
                "3. Preparing for synthesis",
                "",
            ]
        )

    prompt_parts.extend(
        [
            "**Remember:** Call `set_status` at the START of each significant action to update your status for users.",
            "",
            "## What to Do Next",
            "",
            "Choose ONE of these actions:",
            "",
            "**Option A: Explore Data**",
            "- Write Python code to examine data structure, distributions, correlations",
            "- Useful early in investigation or when stuck",
            "",
            "**Option B: Search Literature**",
            "- Query PubMed for papers related to your research question or a specific pattern",
            "- Use this proactively to generate mechanistic hypotheses",
            "",
            "**Option C: Test Hypothesis**",
            "- Write Python code to test a specific hypothesis",
            "- Include appropriate statistical tests, effect size calculations, visualizations",
            "",
            "**Option D: Record Finding**",
            "- If you've confirmed a finding, record it to the knowledge graph",
            "- Include: title, evidence (stats), supporting hypotheses, plots",
            "",
        ]
    )

    if skills_available:
        prompt_parts.extend(
            [
                "**Option E: Use Skill**",
                "- Invoke a skill workflow for structured guidance",
                f"{skills_available}",
                "",
            ]
        )

    prompt_parts.extend(
        [
            "---",
            "",
            "Proceed with your chosen action. Think carefully about what will provide the most insight.",
            "",
            "**Before ending this iteration:** Call `save_iteration_summary` as your FINAL action",
            "to record what you accomplished. The summary should reflect what you actually did,",
            "not what you plan to do next.",
        ]
    )

    return "\n".join(prompt_parts)


def format_skills_list(skills: Dict[str, Dict[str, Any]]) -> str:
    """
    Format available skills for prompt.

    Args:
        skills: Dictionary of skill name -> skill metadata

    Returns:
        Formatted skills list
    """
    if not skills:
        return ""

    lines = ["Available skills:"]
    for skill_name, skill_info in skills.items():
        description = skill_info.get("description", "No description")
        lines.append(f"  - `{skill_name}`: {description}")

    return "\n".join(lines)


async def get_enabled_skills(
    session: AsyncSession,
) -> List[Skill]:
    """
    Get all enabled skills.

    All enabled skills are now available to every job - there is no
    per-job skill selection.

    Args:
        session: Database session

    Returns:
        List of enabled Skill objects
    """
    stmt = (
        select(Skill)
        .where(Skill.is_enabled == True)  # noqa: E712
        .order_by(Skill.category, Skill.name)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def format_skills_for_prompt(
    session: AsyncSession,
) -> str:
    """
    Format skills content for inclusion in an agent prompt.

    Loads all enabled skills and formats them into a single
    string suitable for the agent's context. All enabled skills
    are available to every job.

    Args:
        session: Database session

    Returns:
        Formatted skills content string
    """
    skills = await get_enabled_skills(session)

    if not skills:
        return ""

    parts = [
        "# Domain-Specific Skills",
        "",
        "The following skills have been matched to your research question.",
        "Use these as guidance for analysis approaches and best practices.",
        "",
    ]

    # Group skills by category
    categories: Dict[str, List[Skill]] = {}
    for skill in skills:
        if skill.category not in categories:
            categories[skill.category] = []
        categories[skill.category].append(skill)

    for category, category_skills in sorted(categories.items()):
        parts.append(f"## {category.title()} Skills")
        parts.append("")

        for skill in category_skills:
            parts.append(f"### {skill.name}")
            if skill.description:
                parts.append(f"*{skill.description}*")
            parts.append("")
            parts.append(skill.content)
            parts.append("")

    return "\n".join(parts)


def format_skills_summary(skills: List[Skill]) -> str:
    """
    Format a summary list of skills for prompt display.

    Args:
        skills: List of Skill objects

    Returns:
        Formatted summary string
    """
    if not skills:
        return ""

    lines = ["Available skills:"]
    for skill in skills:
        desc = skill.description or "No description"
        lines.append(f"  - `{skill.category}/{skill.slug}`: {desc}")

    return "\n".join(lines)
