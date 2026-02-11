"""
Skill relevance matching service.

Two-stage approach:
1. Pre-filter with PostgreSQL full-text search (GIN index)
2. Score candidates with Anthropic API for semantic relevance
"""

import logging
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .database.models import Skill
from .settings import get_settings

# Import anthropic at module level for mocking in tests
try:
    import anthropic  # type: ignore[import-not-found]
except ImportError:
    anthropic: Any = None  # type: ignore[no-redef]

logger = logging.getLogger(__name__)

# Default number of candidates to retrieve from full-text search
DEFAULT_CANDIDATE_LIMIT = 20

# Default minimum score threshold for matching
DEFAULT_SCORE_THRESHOLD = 0.3

# Maximum number of skills to return
DEFAULT_MAX_RESULTS = 5


@dataclass
class ScoredSkill:
    """A skill with its relevance score."""

    skill_id: UUID
    name: str
    category: str
    slug: str
    description: str | None
    score: float
    match_reason: str


class SkillRelevanceService:
    """
    Service for finding relevant skills for a research prompt.

    Uses a two-stage approach:
    1. Pre-filter: PostgreSQL full-text search with GIN index (~20 candidates)
    2. Score: Anthropic API semantic scoring in a single batch call
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-3-5-sonnet-20241022",
    ):
        """
        Initialize the skill relevance service.

        Args:
            api_key: Anthropic API key (uses ANTHROPIC_API_KEY env var if not provided)
            model: Model to use for scoring (default: claude-3-5-sonnet)
        """
        self.api_key = api_key or get_settings().provider.anthropic_api_key
        self.model = model

    async def find_relevant_skills(
        self,
        session: AsyncSession,
        prompt: str,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        score_threshold: float = DEFAULT_SCORE_THRESHOLD,
        max_results: int = DEFAULT_MAX_RESULTS,
    ) -> list[ScoredSkill]:
        """
        Find skills relevant to a research prompt.

        Args:
            session: Database session
            prompt: Research question or job description
            candidate_limit: Max candidates from pre-filter stage
            score_threshold: Minimum relevance score (0.0-1.0)
            max_results: Maximum skills to return

        Returns:
            List of ScoredSkill objects, sorted by score descending
        """
        # Stage 1: Pre-filter with full-text search
        candidates = await self._prefilter_skills(session, prompt, candidate_limit)

        if not candidates:
            logger.info("No candidate skills found for prompt")
            return []

        logger.info(
            "Found %d candidate skills from full-text search",
            len(candidates),
        )

        # Stage 2: Score with Anthropic API
        scored = await self._score_skills_batch(prompt, candidates)

        # Filter by threshold and limit results
        result = [s for s in scored if s.score >= score_threshold]
        result.sort(key=lambda x: x.score, reverse=True)

        return result[:max_results]

    async def _prefilter_skills(
        self,
        session: AsyncSession,
        prompt: str,
        limit: int,
    ) -> list[Skill]:
        """
        Pre-filter skills using PostgreSQL full-text search.

        Uses plainto_tsquery for natural language query conversion
        and the GIN-indexed search_vector column.

        Args:
            session: Database session
            prompt: Search query
            limit: Maximum results

        Returns:
            List of candidate Skill objects
        """
        # Use plainto_tsquery for natural language -> tsquery conversion
        tsquery = func.plainto_tsquery("english", prompt)

        # Query with full-text search ranking
        stmt = (
            select(Skill)
            .where(
                Skill.is_enabled.is_(True),
                Skill.search_vector.op("@@")(tsquery),
            )
            .order_by(
                func.ts_rank(Skill.search_vector, tsquery).desc(),
            )
            .limit(limit)
        )

        result = await session.execute(stmt)
        return list(result.scalars().all())

    async def _score_skills_batch(
        self,
        prompt: str,
        candidates: list[Skill],
    ) -> list[ScoredSkill]:
        """
        Score candidate skills using Anthropic API.

        Makes a single API call to score all candidates efficiently.

        Args:
            prompt: Research prompt
            candidates: List of candidate skills to score

        Returns:
            List of ScoredSkill objects
        """
        if not self.api_key:
            logger.warning("No Anthropic API key configured, using fallback text similarity")
            return self._fallback_scoring(prompt, candidates)

        if anthropic is None:
            logger.warning("anthropic package not installed, using fallback text similarity")
            return self._fallback_scoring(prompt, candidates)

        # Build the scoring prompt
        skill_descriptions = []
        for i, skill in enumerate(candidates, 1):
            desc = skill.description or "(No description)"
            skill_descriptions.append(f"{i}. **{skill.name}** ({skill.category}): {desc}")

        system_prompt = """You are a scientific research assistant helping match domain-specific skills to research questions.

Your task is to score how relevant each skill is to the given research prompt on a scale of 0.0 to 1.0.

Scoring guidelines:
- 1.0: Perfect match - skill directly addresses the research question
- 0.7-0.9: Strong match - skill is highly relevant to the domain/methods
- 0.4-0.6: Moderate match - skill provides some useful context
- 0.1-0.3: Weak match - tangentially related
- 0.0: No relevance

Respond with JSON only, in this exact format:
{
  "scores": [
    {"skill_num": 1, "score": 0.85, "reason": "Brief explanation"},
    {"skill_num": 2, "score": 0.3, "reason": "Brief explanation"},
    ...
  ]
}"""

        user_prompt = f"""Research prompt:
{prompt}

Available skills:
{chr(10).join(skill_descriptions)}

Score each skill's relevance to this research prompt."""

        try:
            client = anthropic.Anthropic(api_key=self.api_key)

            response = client.messages.create(
                model=self.model,
                max_tokens=2000,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}],
            )

            response_text = response.content[0].text  # type: ignore[union-attr]

            # Parse JSON response
            import json

            # Handle potential markdown code blocks
            if "```json" in response_text:
                response_text = response_text.split("```json")[1].split("```")[0]
            elif "```" in response_text:
                response_text = response_text.split("```")[1].split("```")[0]

            data = json.loads(response_text.strip())
            scores_data = data.get("scores", [])

            # Build scored skill list
            scored_skills = []
            for score_info in scores_data:
                skill_num = score_info.get("skill_num", 0)
                if 1 <= skill_num <= len(candidates):
                    skill = candidates[skill_num - 1]
                    scored_skills.append(
                        ScoredSkill(
                            skill_id=skill.id,
                            name=skill.name,
                            category=skill.category,
                            slug=skill.slug,
                            description=skill.description,
                            score=float(score_info.get("score", 0)),
                            match_reason=score_info.get("reason", ""),
                        )
                    )

            return scored_skills

        except Exception as e:
            logger.warning("Anthropic API scoring failed: %s, using fallback", e)
            return self._fallback_scoring(prompt, candidates)

    def _fallback_scoring(
        self,
        prompt: str,
        candidates: list[Skill],
    ) -> list[ScoredSkill]:
        """
        Fallback scoring using simple text similarity.

        Uses word overlap between prompt and skill metadata.

        Args:
            prompt: Research prompt
            candidates: List of candidate skills

        Returns:
            List of ScoredSkill objects
        """
        prompt_words = set(prompt.lower().split())

        scored = []
        for skill in candidates:
            # Combine skill text for matching
            skill_text = " ".join(
                filter(
                    None,
                    [
                        skill.name,
                        skill.category,
                        skill.description or "",
                        " ".join(skill.tags) if skill.tags else "",
                    ],
                )
            )
            skill_words = set(skill_text.lower().split())

            # Calculate Jaccard similarity
            if prompt_words and skill_words:
                intersection = len(prompt_words & skill_words)
                union = len(prompt_words | skill_words)
                score = intersection / union if union > 0 else 0.0
            else:
                score = 0.0

            scored.append(
                ScoredSkill(
                    skill_id=skill.id,
                    name=skill.name,
                    category=skill.category,
                    slug=skill.slug,
                    description=skill.description,
                    score=score,
                    match_reason="Matched by text similarity",
                )
            )

        return scored


async def get_all_categories(session: AsyncSession) -> list[str]:
    """
    Get all unique skill categories.

    Args:
        session: Database session

    Returns:
        List of category names
    """
    stmt = (
        select(Skill.category)
        .where(Skill.is_enabled == True)  # noqa: E712
        .distinct()
        .order_by(Skill.category)
    )
    result = await session.execute(stmt)
    return [row[0] for row in result.all()]


async def get_skills_by_category(
    session: AsyncSession,
    category: str,
) -> list[Skill]:
    """
    Get all skills in a category.

    Args:
        session: Database session
        category: Category name

    Returns:
        List of Skill objects
    """
    stmt = (
        select(Skill)
        .where(
            Skill.is_enabled == True,  # noqa: E712
            Skill.category == category,
        )
        .order_by(Skill.name)
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def search_skills(
    session: AsyncSession,
    query: str,
    category: str | None = None,
    tags: list[str] | None = None,
    limit: int = 50,
) -> list[Skill]:
    """
    Search skills with optional filters.

    Args:
        session: Database session
        query: Search query (uses full-text search)
        category: Optional category filter
        tags: Optional tag filter (skills must have all specified tags)
        limit: Maximum results

    Returns:
        List of matching Skill objects
    """
    tsquery = func.plainto_tsquery("english", query)

    conditions = [
        Skill.is_enabled == True,  # noqa: E712
        Skill.search_vector.op("@@")(tsquery),
    ]

    if category:
        conditions.append(Skill.category == category)

    if tags:
        # Skills must contain all specified tags
        for tag in tags:
            conditions.append(Skill.tags.contains([tag]))

    stmt = (
        select(Skill)
        .where(*conditions)
        .order_by(func.ts_rank(Skill.search_vector, tsquery).desc())
        .limit(limit)
    )

    result = await session.execute(stmt)
    return list(result.scalars().all())
