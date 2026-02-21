"""
Skill ingestion service for parsing and syncing skills.

Handles parsing markdown skills with YAML frontmatter and syncing
skills from external sources like GitHub repositories.
"""

import hashlib
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database.models import Skill, SkillSource

logger = logging.getLogger(__name__)

# =============================================================================
# Base Class for Extensible Ingesters
# =============================================================================


class BaseSkillIngester(ABC):
    """
    Abstract base class for skill ingesters.

    To add a new source type:
    1. Create a class that inherits from BaseSkillIngester
    2. Implement the sync_source() method
    3. Register it with register_ingester("source_type", YourIngester)

    Example:
        class S3SkillIngester(BaseSkillIngester):
            async def sync_source(self, session, source, **kwargs):
                # Fetch skills from S3 bucket
                ...

        register_ingester("s3", S3SkillIngester)
    """

    @abstractmethod
    async def sync_source(
        self,
        session: AsyncSession,
        source: SkillSource,
        **kwargs: Any,
    ) -> dict[str, int]:
        """
        Sync skills from a source.

        Args:
            session: Database session
            source: SkillSource to sync from
            **kwargs: Additional arguments (e.g., github_token)

        Returns:
            Dict with counts: {'created': N, 'updated': N, 'unchanged': N, 'errors': N}
        """

    async def close(self) -> None:
        """Optional cleanup method for ingesters with resources."""


@dataclass
class ParsedSkill:
    """Parsed skill from a markdown file."""

    name: str
    slug: str
    category: str
    description: str | None
    content: str
    tags: list[str]
    source_path: str
    content_hash: str


class SkillParseError(Exception):
    """Error parsing a skill file."""


class SkillParser:
    """
    Parser for skill markdown files with YAML frontmatter.

    Skills are markdown files with YAML frontmatter containing metadata:

    ```markdown
    ---
    name: Metabolomics Analysis
    category: metabolomics
    description: Statistical analysis of metabolomics data
    tags:
      - statistics
      - metabolomics
    ---

    # Metabolomics Analysis

    Content here...
    ```
    """

    FRONTMATTER_PATTERN = re.compile(
        r"^---\s*\n(.*?)\n---\s*\n(.*)$",
        re.DOTALL,
    )

    def parse_file(self, path: Path) -> ParsedSkill:
        """
        Parse a skill markdown file.

        Args:
            path: Path to the markdown file

        Returns:
            ParsedSkill object with parsed metadata and content

        Raises:
            SkillParseError: If the file cannot be parsed
        """
        content = path.read_text(encoding="utf-8")
        return self.parse_content(content, str(path))

    def parse_content(self, content: str, source_path: str) -> ParsedSkill:
        """
        Parse skill content with YAML frontmatter.

        Args:
            content: Raw markdown content
            source_path: Path for error reporting and slug generation

        Returns:
            ParsedSkill object

        Raises:
            SkillParseError: If parsing fails
        """
        match = self.FRONTMATTER_PATTERN.match(content)
        if not match:
            raise SkillParseError(
                f"Invalid skill format in {source_path}: Missing or malformed YAML frontmatter"
            )

        frontmatter_raw, body = match.groups()

        try:
            frontmatter = yaml.safe_load(frontmatter_raw)
        except yaml.YAMLError as e:
            raise SkillParseError(f"Invalid YAML frontmatter in {source_path}: {e}") from e

        if not isinstance(frontmatter, dict):
            raise SkillParseError(f"YAML frontmatter must be a mapping in {source_path}")

        # Extract required fields
        name = frontmatter.get("name")
        if not name:
            raise SkillParseError(f"Missing 'name' in frontmatter: {source_path}")

        category = frontmatter.get("category")
        if not category:
            # Derive category from path structure
            # Use immediate parent directory as category (e.g., "biopython" from "skills/biopython/SKILL.md")
            path_parts = Path(source_path).parts
            if len(path_parts) >= 2:
                category = path_parts[-2]
            else:
                raise SkillParseError(
                    f"Missing 'category' in frontmatter and cannot derive from path: {source_path}"
                )

        # Generate slug from filename or name
        slug = frontmatter.get("slug")
        if not slug:
            filename_stem = Path(source_path).stem
            # For generic filenames (SKILL.md, README.md, INDEX.md), use parent directory
            if filename_stem.upper() in ("SKILL", "README", "INDEX"):
                path_parts = Path(source_path).parts
                if len(path_parts) >= 2:
                    slug = path_parts[-2]
                else:
                    slug = filename_stem
            else:
                slug = filename_stem
            # Sanitize slug
            slug = re.sub(r"[^a-z0-9-]", "-", slug.lower())
            slug = re.sub(r"-+", "-", slug).strip("-")

        # Optional fields
        description = frontmatter.get("description")
        tags = frontmatter.get("tags", [])
        if not isinstance(tags, list):
            tags = [tags] if tags else []

        # Compute content hash
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        return ParsedSkill(
            name=name,
            slug=slug,
            category=category,
            description=description,
            content=body.strip(),
            tags=tags,
            source_path=source_path,
            content_hash=content_hash,
        )


class GitHubSkillIngester(BaseSkillIngester):
    """
    Ingests skills from a GitHub repository.

    Uses GitHub's REST API to fetch skill files from a repository
    and parse them into the database.
    """

    GITHUB_API_BASE = "https://api.github.com"

    def __init__(
        self,
        github_token: str | None = None,
        parser: SkillParser | None = None,
    ):
        """
        Initialize the GitHub skill ingester.

        Args:
            github_token: Optional GitHub personal access token for higher rate limits
            parser: Optional SkillParser instance (creates default if not provided)
        """
        self.github_token = github_token
        self.parser = parser or SkillParser()
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            headers = {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "SHANDY-SkillIngester",
            }
            if self.github_token:
                headers["Authorization"] = f"Bearer {self.github_token}"

            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _parse_github_url(self, url: str) -> tuple[str, str]:
        """
        Parse owner and repo from a GitHub URL.

        Args:
            url: GitHub repository URL

        Returns:
            Tuple of (owner, repo)

        Raises:
            ValueError: If URL is not a valid GitHub repo URL
        """
        patterns = [
            r"github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$",
            r"github\.com/([^/]+)/([^/]+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                return match.group(1), match.group(2).rstrip(".git")

        raise ValueError(f"Invalid GitHub URL: {url}")

    async def get_latest_commit(self, owner: str, repo: str, branch: str) -> str:
        """
        Get the latest commit SHA for a branch.

        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name

        Returns:
            Commit SHA string
        """
        client = await self._get_client()
        url = f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{branch}"
        response = await client.get(url)
        response.raise_for_status()
        sha: str = response.json()["sha"]
        return sha

    async def list_skill_files(
        self,
        owner: str,
        repo: str,
        branch: str,
        skills_path: str = "",
    ) -> list[dict[str, Any]]:
        """
        List all markdown files in the skills directory.

        Args:
            owner: Repository owner
            repo: Repository name
            branch: Branch name
            skills_path: Subdirectory containing skills

        Returns:
            List of file metadata dicts with 'path' and 'download_url'
        """
        client = await self._get_client()

        # Get the tree recursively
        url = f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/git/trees/{branch}"
        params = {"recursive": "1"}
        response = await client.get(url, params=params)
        response.raise_for_status()

        tree = response.json().get("tree", [])
        skill_files = []

        prefix = skills_path.strip("/") + "/" if skills_path else ""

        for item in tree:
            path = item.get("path", "")
            if item.get("type") != "blob":
                continue
            if not path.lower().endswith("/skill.md"):
                continue
            if prefix and not path.startswith(prefix):
                continue

            # Build raw content URL
            download_url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
            skill_files.append(
                {
                    "path": path,
                    "download_url": download_url,
                    "sha": item.get("sha"),
                }
            )

        return skill_files

    async def fetch_file_content(self, download_url: str) -> str:
        """
        Fetch the content of a file from GitHub.

        Args:
            download_url: Raw content URL

        Returns:
            File content as string
        """
        client = await self._get_client()
        response = await client.get(download_url)
        response.raise_for_status()
        return response.text

    async def sync_source(
        self,
        session: AsyncSession,
        source: SkillSource,
        **kwargs: Any,
    ) -> dict[str, int]:
        """
        Sync skills from a GitHub source.

        Args:
            session: Database session
            source: SkillSource to sync from
            **kwargs: Additional arguments (unused)

        Returns:
            Dict with counts: {'created': N, 'updated': N, 'unchanged': N, 'errors': N}
        """
        if source.source_type != "github":
            raise ValueError(f"Source {source.id} is not a GitHub source")

        if not source.url:
            raise ValueError(f"Source {source.id} has no URL")

        owner, repo = self._parse_github_url(source.url)
        branch = source.branch
        force = kwargs.get("force", False)

        # Get latest commit
        commit_sha = await self.get_latest_commit(owner, repo, branch)

        # Short-circuit: skip if commit SHA unchanged (unless forced)
        if not force and source.last_commit_sha == commit_sha:
            logger.info("Skipping %s: commit unchanged (%s)", source.name, commit_sha[:12])
            source.last_synced_at = datetime.now(timezone.utc)
            source.sync_error = None
            await session.commit()
            return {
                "created": 0,
                "updated": 0,
                "unchanged": 0,
                "errors": 0,
                "skipped_reason": "commit_unchanged",  # type: ignore[dict-item]
            }

        # List skill files
        skill_files = await self.list_skill_files(owner, repo, branch, source.skills_path)

        stats = {"created": 0, "updated": 0, "unchanged": 0, "errors": 0}

        # Session is expected to be an admin session (bypasses RLS)
        for file_info in skill_files:
            try:
                content = await self.fetch_file_content(file_info["download_url"])
                parsed = self.parser.parse_content(content, file_info["path"])

                # Check if skill exists
                stmt = select(Skill).where(
                    Skill.source_id == source.id,
                    Skill.source_path == file_info["path"],
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing:
                    if existing.content_hash == parsed.content_hash:
                        stats["unchanged"] += 1
                        continue

                    # Update existing skill
                    existing.name = parsed.name
                    existing.slug = parsed.slug
                    existing.category = parsed.category
                    existing.description = parsed.description
                    existing.content = parsed.content
                    existing.tags = parsed.tags
                    existing.content_hash = parsed.content_hash
                    existing.commit_sha = commit_sha
                    existing.version = existing.version + 1
                    stats["updated"] += 1
                else:
                    # Create new skill
                    skill = Skill(
                        name=parsed.name,
                        slug=parsed.slug,
                        category=parsed.category,
                        description=parsed.description,
                        content=parsed.content,
                        tags=parsed.tags,
                        source_id=source.id,
                        source_path=file_info["path"],
                        content_hash=parsed.content_hash,
                        commit_sha=commit_sha,
                    )
                    session.add(skill)
                    stats["created"] += 1

            except (SkillParseError, httpx.HTTPError) as e:
                stats["errors"] += 1
                # Log error but continue
                logger.warning("Error processing %s: %s", file_info["path"], e)

        # Update source metadata
        source.last_synced_at = datetime.now(timezone.utc)
        source.last_commit_sha = commit_sha
        source.sync_error = None

        await session.commit()

        return stats


class LocalSkillIngester(BaseSkillIngester):
    """
    Ingests skills from a local filesystem directory.

    Scans a directory for markdown skill files and syncs them to the database.
    """

    def __init__(self, parser: SkillParser | None = None):
        """
        Initialize the local skill ingester.

        Args:
            parser: Optional SkillParser instance
        """
        self.parser = parser or SkillParser()

    async def sync_source(
        self,
        session: AsyncSession,
        source: SkillSource,
        **kwargs: Any,
    ) -> dict[str, int]:
        """
        Sync skills from a local directory source.

        Args:
            session: Database session
            source: SkillSource to sync from
            **kwargs: Additional arguments (unused)

        Returns:
            Dict with counts: {'created': N, 'updated': N, 'unchanged': N, 'errors': N}
        """
        if source.source_type != "local":
            raise ValueError(f"Source {source.id} is not a local source")

        if not source.path:
            raise ValueError(f"Source {source.id} has no path")

        base_path = Path(source.path)
        if source.skills_path:
            base_path = base_path / source.skills_path

        if not base_path.exists():
            raise ValueError(f"Path does not exist: {base_path}")

        stats = {"created": 0, "updated": 0, "unchanged": 0, "errors": 0}

        # Session is expected to be an admin session (bypasses RLS)
        # Only process SKILL.md files (skip reference docs, READMEs, etc.)
        for md_file in base_path.rglob("**/SKILL.md"):
            try:
                relative_path = str(md_file.relative_to(Path(source.path)))
                parsed = self.parser.parse_file(md_file)

                # Check if skill exists from this source
                stmt = select(Skill).where(
                    Skill.source_id == source.id,
                    Skill.source_path == relative_path,
                )
                result = await session.execute(stmt)
                existing = result.scalar_one_or_none()

                if existing:
                    if existing.content_hash == parsed.content_hash:
                        stats["unchanged"] += 1
                        continue

                    # Update existing skill
                    existing.name = parsed.name
                    existing.slug = parsed.slug
                    existing.category = parsed.category
                    existing.description = parsed.description
                    existing.content = parsed.content
                    existing.tags = parsed.tags
                    existing.content_hash = parsed.content_hash
                    existing.version = existing.version + 1
                    stats["updated"] += 1
                else:
                    # Check if skill with same category/slug exists globally
                    stmt_global = select(Skill).where(
                        Skill.category == parsed.category,
                        Skill.slug == parsed.slug,
                    )
                    result_global = await session.execute(stmt_global)
                    existing_global = result_global.scalar_one_or_none()

                    if existing_global:
                        # Skip - skill already exists from another source
                        logger.info(
                            "Skipping %s: skill %s/%s already exists from source %s",
                            relative_path,
                            parsed.category,
                            parsed.slug,
                            existing_global.source_id,
                        )
                        stats["unchanged"] += 1
                        continue

                    # Create new skill
                    skill = Skill(
                        name=parsed.name,
                        slug=parsed.slug,
                        category=parsed.category,
                        description=parsed.description,
                        content=parsed.content,
                        tags=parsed.tags,
                        source_id=source.id,
                        source_path=relative_path,
                        content_hash=parsed.content_hash,
                    )
                    session.add(skill)
                    stats["created"] += 1

            except SkillParseError as e:
                stats["errors"] += 1
                logger.warning("Error processing %s: %s", md_file, e)

        # Update source metadata
        source.last_synced_at = datetime.now(timezone.utc)
        source.sync_error = None

        await session.commit()

        return stats


# =============================================================================
# Ingester Registry - Extensible pattern for adding new source types
# =============================================================================

# Registry mapping source_type -> ingester class
_INGESTER_REGISTRY: dict[str, type[BaseSkillIngester]] = {}


def register_ingester(
    source_type: str,
    ingester_class: type[BaseSkillIngester],
) -> None:
    """
    Register a skill ingester for a source type.

    Args:
        source_type: Source type string (e.g., "github", "local", "s3")
        ingester_class: Class that inherits from BaseSkillIngester
    """
    _INGESTER_REGISTRY[source_type] = ingester_class


# Register built-in ingesters
register_ingester("github", GitHubSkillIngester)
register_ingester("local", LocalSkillIngester)


async def sync_skill_source(
    session: AsyncSession,
    source: SkillSource,
    github_token: str | None = None,
    **kwargs,
) -> dict[str, int]:
    """
    Sync skills from a source based on its type.

    Uses the ingester registry to support extensible source types.
    New source types can be added by calling register_ingester().

    Args:
        session: Database session
        source: SkillSource to sync
        github_token: Optional GitHub token for API access
        **kwargs: Additional arguments passed to the ingester

    Returns:
        Dict with sync statistics

    Raises:
        ValueError: If source type is not registered
    """
    source_type = source.source_type

    if source_type not in _INGESTER_REGISTRY:
        registered = ", ".join(_INGESTER_REGISTRY.keys()) or "(none)"
        raise ValueError(
            f"Unknown source type: {source_type}. "
            f"Registered types: {registered}. "
            f"Use register_ingester() to add new types."
        )

    # Get the ingester class and instantiate
    ingester_class = _INGESTER_REGISTRY[source_type]

    # Pass appropriate kwargs based on source type
    # Type ignore: concrete ingesters have specific init signatures
    if source_type == "github":
        ingester = ingester_class(github_token=github_token)  # type: ignore[call-arg]
    else:
        ingester = ingester_class()

    try:
        return await ingester.sync_source(session, source, **kwargs)
    finally:
        await ingester.close()
