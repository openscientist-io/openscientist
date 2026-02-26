"""
Prompt templates for SHANDY orchestrator.

System prompts and discovery iteration prompts for the autonomous agent.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database.models import Skill


def get_system_prompt() -> str:
    """
    Get system prompt for Claude.

    Returns:
        System prompt string
    """
    return """You are an autonomous scientific discovery agent. Your goal is to discover mechanistic insights from scientific data through iterative hypothesis testing.

**Your Capabilities:**

You have access to tools:
- `execute_code`: Run code to analyze data. Supports `language="python"` (default, with pandas, polars, numpy, scipy, matplotlib, seaborn, plotly, statsmodels, pingouin, sklearn, umap-learn, leidenalg, networkx, biopython, scanpy, pydeseq2, and more), `language="rust"` (compiled via cargo; pre-seeded crates: rayon, ndarray, ndarray-stats, statrs, rand, serde_json, csv, anyhow, itertools, num-traits), or `language="sparql"` (query a remote SPARQL endpoint — include `# ENDPOINT: <url>` in the query)
- `search_pubmed`: Search scientific literature for relevant papers
- `update_knowledge_state`: Record a confirmed finding
- `set_status`: Update your current status message (e.g., "Analyzing correlation between X and Y")
- `set_job_title`: Set a brief title for this job (e.g., "Kinase inhibitor binding analysis")

IMPORTANT:
- Call `set_job_title` early (iteration 1) to give the job a meaningful, concise title
- Call `set_status` at the START of each significant action to let users know what you're working on

Domain-specific analysis skills are in `.claude/skills/`. List the directory and read relevant skill files before starting your analysis.

**Your Approach:**

1. **Explore** the data to identify patterns
2. **Generate hypotheses** using literature and domain knowledge (use skills for guidance)
3. **Test hypotheses** by writing Python code for statistical analyses
4. **Interpret results** - both positive AND negative findings are valuable
5. **Iterate** - use findings to generate new hypotheses
6. **Learn from failures** - rejected hypotheses guide future investigation

**Important Principles:**

- Write clear, well-documented code; use Python by default, Rust for performance-critical computation, SPARQL for knowledge base queries
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
    skills_available: str | None = None,
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
            "- Write code to examine data structure, distributions, correlations",
            "- Use `language='python'` (default) for most analysis",
            "- Use `language='rust'` for performance-critical computation (e.g., tight loops over >1M rows);",
            "  pre-seeded crates: rayon, ndarray, ndarray-stats, statrs, rand, serde_json, csv, anyhow, itertools",
            "- Useful early in investigation or when stuck",
            "",
            "**Option B: Search Literature**",
            "- Query PubMed for papers related to your research question or a specific pattern",
            "- Use this proactively to generate mechanistic hypotheses",
            "",
            "**Option C: Query Knowledge Base**",
            "- Use `language='sparql'` to query structured knowledge bases for biological,",
            "  chemical, or scientific facts (gene functions, protein interactions, drug targets,",
            "  taxonomic relationships, etc.)",
            "- Include `# ENDPOINT: <url>` in the query (e.g., https://query.wikidata.org/sparql",
            "  or https://sparql.uniprot.org/sparql)",
            "- Useful when you need structured facts not found in PubMed abstracts",
            "",
            "**Option D: Test Hypothesis**",
            "- Write code to test a specific hypothesis",
            "- Use `language='python'` for statistical tests, effect sizes, visualizations",
            "- Use `language='rust'` for performance-critical computation;",
            "  pre-seeded crates: rayon, ndarray, ndarray-stats, statrs, rand, serde_json, csv, anyhow, itertools",
            "",
            "**Option E: Record Finding**",
            "- If you've confirmed a finding, record it to the knowledge graph",
            "- Include: title, evidence (stats), supporting hypotheses, plots",
            "",
        ]
    )

    if skills_available:
        prompt_parts.extend(
            [
                "**Option F: Use Skill**",
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


def format_skills_list(skills: dict[str, dict[str, Any]]) -> str:
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
) -> list[Skill]:
    """
    Get all enabled skills.

    All enabled skills are now available to every job - there is no
    per-job skill selection.

    Args:
        session: Database session

    Returns:
        List of enabled Skill objects
    """
    stmt = select(Skill).where(Skill.is_enabled.is_(True)).order_by(Skill.category, Skill.name)
    result = await session.execute(stmt)
    return list(result.scalars().all())
