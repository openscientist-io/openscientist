"""
Skill ingestion service for parsing and syncing skills.
"""

import asyncio
import hashlib
import io
import logging
import re
import tarfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import httpx
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database.models import Skill, SkillSource

logger = logging.getLogger(__name__)


class BaseSkillIngester(ABC):
    """Abstract base class for skill ingesters.

    To add a new source type, subclass it, implement ``sync_source`` and
    register the class with ``register_ingester("source_type", YourIngester)``.
    """

    @abstractmethod
    async def sync_source(
        self,
        session: AsyncSession,
        source: SkillSource,
        **kwargs: Any,
    ) -> dict[str, int]:
        """Sync skills from a source.

        Returns counts with keys created, updated, unchanged and errors.
        """

    async def close(self) -> None:
        """Optional cleanup method for ingesters with resources."""
        logger.debug("No ingester resources to close for %s", self.__class__.__name__)


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
    """Parser for skill markdown files with YAML frontmatter.

    The frontmatter must be a mapping with ``name`` and ``category`` (which
    falls back to the parent directory name), plus optional ``description``,
    ``slug`` and ``tags``.
    """

    FRONTMATTER_PATTERN = re.compile(
        r"^---\s*\n(.*?)\n---\s*\n(.*)$",
        re.DOTALL,
    )

    def parse_file(self, path: Path) -> ParsedSkill:
        """Parse a skill markdown file."""
        content = path.read_text(encoding="utf-8")
        return self.parse_content(content, str(path))

    def parse_content(self, content: str, source_path: str) -> ParsedSkill:
        """Parse skill content with YAML frontmatter."""
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

        name = frontmatter.get("name")
        if not name:
            raise SkillParseError(f"Missing 'name' in frontmatter: {source_path}")

        category = frontmatter.get("category")
        if not category:
            # Derive the category from the immediate parent directory name.
            path_parts = Path(source_path).parts
            if len(path_parts) >= 2:
                category = path_parts[-2]
            else:
                raise SkillParseError(
                    f"Missing 'category' in frontmatter and cannot derive from path: {source_path}"
                )

        slug = frontmatter.get("slug")
        if not slug:
            filename_stem = Path(source_path).stem
            # Generic filenames (SKILL.md, README.md, INDEX.md) fall back to the parent directory.
            if filename_stem.upper() in ("SKILL", "README", "INDEX"):
                path_parts = Path(source_path).parts
                slug = path_parts[-2] if len(path_parts) >= 2 else filename_stem
            else:
                slug = filename_stem
            slug = re.sub(r"[^a-z0-9-]", "-", slug.lower())
            slug = re.sub(r"-+", "-", slug).strip("-")

        description = frontmatter.get("description")
        tags = frontmatter.get("tags", [])
        if not isinstance(tags, list):
            tags = [tags] if tags else []

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
    """Ingests skills from a GitHub repository via its REST API."""

    GITHUB_API_BASE = "https://api.github.com"
    MIN_REQUEST_INTERVAL_SECONDS = 1.0
    MAX_429_RETRIES = 3

    def __init__(
        self,
        github_token: str | None = None,
        parser: SkillParser | None = None,
    ):
        """Initialize the GitHub skill ingester.

        A github_token raises the API rate limit.
        """
        self.github_token = github_token
        self.parser = parser or SkillParser()
        self._client: httpx.AsyncClient | None = None
        self._last_request_at: float | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None:
            headers = {
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "OpenScientist-SkillIngester",
            }
            if self.github_token:
                headers["Authorization"] = f"Bearer {self.github_token}"

            self._client = httpx.AsyncClient(
                headers=headers,
                timeout=30.0,
                follow_redirects=True,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _sleep_between_requests(self) -> None:
        """Pace outbound GitHub requests to reduce rate-limit pressure."""
        if self._last_request_at is None:
            return

        elapsed = time.monotonic() - self._last_request_at
        remaining = self.MIN_REQUEST_INTERVAL_SECONDS - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)

    @staticmethod
    def _retry_delay_from_response(response: httpx.Response, attempt: int) -> float:
        """Use Retry-After when available, otherwise fall back to backoff."""
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(float(retry_after), 0.0)
            except ValueError:
                pass

        return min(float(2 ** (attempt - 1)), 30.0)

    async def _github_get(self, url: str, **kwargs: Any) -> httpx.Response:
        """GET with pacing and bounded retry on 429 responses."""
        client = await self._get_client()

        for attempt in range(1, self.MAX_429_RETRIES + 2):
            await self._sleep_between_requests()
            response = await client.get(url, **kwargs)
            self._last_request_at = time.monotonic()

            if response.status_code != 429:
                return response

            if attempt > self.MAX_429_RETRIES:
                return response

            delay = self._retry_delay_from_response(response, attempt)
            logger.warning(
                "GitHub rate limit hit for %s; sleeping %.1f seconds before retry %d/%d",
                url,
                delay,
                attempt,
                self.MAX_429_RETRIES,
            )
            await asyncio.sleep(delay)
            self._last_request_at = None

        return response

    def _parse_github_url(self, url: str) -> tuple[str, str]:
        """Parse owner and repo from a GitHub URL."""
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
        """Get the latest commit SHA for a branch."""
        url = f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{branch}"
        response = await self._github_get(url)
        response.raise_for_status()
        sha: str = response.json()["sha"]
        return sha

    def _extract_skill_files_from_archive(
        self,
        archive_bytes: bytes,
        skills_path: str = "",
    ) -> list[dict[str, Any]]:
        """Extract SKILL.md files from a GitHub tarball."""
        prefix = skills_path.strip("/")
        prefix_with_sep = f"{prefix}/" if prefix else ""
        skill_files: list[dict[str, Any]] = []

        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:gz") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue

                member_path = PurePosixPath(member.name)
                if len(member_path.parts) < 2:
                    continue

                repo_relative_path = str(PurePosixPath(*member_path.parts[1:]))
                if not repo_relative_path.lower().endswith("/skill.md"):
                    continue
                if prefix and not repo_relative_path.startswith(prefix_with_sep):
                    continue

                extracted = archive.extractfile(member)
                if extracted is None:
                    continue

                skill_files.append(
                    {
                        "path": repo_relative_path,
                        "content_bytes": extracted.read(),
                    }
                )

        return skill_files

    async def fetch_skill_files_from_archive(
        self,
        owner: str,
        repo: str,
        branch: str,
        skills_path: str = "",
    ) -> list[dict[str, Any]]:
        """Fetch all SKILL.md files from a single repository tarball.

        One archive fetch avoids the per-file raw.githubusercontent.com
        requests that draw 429s.
        """
        url = f"{self.GITHUB_API_BASE}/repos/{owner}/{repo}/tarball/{branch}"
        response = await self._github_get(url, follow_redirects=True)
        response.raise_for_status()
        return self._extract_skill_files_from_archive(response.content, skills_path)

    async def sync_source(
        self,
        session: AsyncSession,
        source: SkillSource,
        **kwargs: Any,
    ) -> dict[str, int]:
        """Sync skills from a GitHub source."""
        if source.source_type != "github":
            raise ValueError(f"Source {source.id} is not a GitHub source")

        if not source.url:
            raise ValueError(f"Source {source.id} has no URL")

        owner, repo = self._parse_github_url(source.url)
        branch = source.branch
        force = kwargs.get("force", False)

        commit_sha = await self.get_latest_commit(owner, repo, branch)

        if not force and source.last_commit_sha == commit_sha:
            logger.info("Skipping %s: commit unchanged (%s)", source.name, commit_sha[:12])
            source.last_synced_at = datetime.now(UTC)
            source.sync_error = None
            await session.commit()
            return {
                "created": 0,
                "updated": 0,
                "unchanged": 0,
                "errors": 0,
                "skipped_reason": "commit_unchanged",  # type: ignore[dict-item]
            }

        skill_files = await self.fetch_skill_files_from_archive(
            owner,
            repo,
            branch,
            source.skills_path,
        )

        stats = {"created": 0, "updated": 0, "unchanged": 0, "errors": 0}

        # Session is expected to be an admin session (bypasses RLS)
        for file_info in skill_files:
            try:
                content = file_info["content_bytes"].decode("utf-8")
                parsed = self.parser.parse_content(content, file_info["path"])

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

            except (SkillParseError, UnicodeDecodeError) as e:
                stats["errors"] += 1
                logger.warning("Error processing %s: %s", file_info["path"], e)

        source.last_synced_at = datetime.now(UTC)
        source.last_commit_sha = commit_sha
        source.sync_error = None

        await session.commit()

        return stats


class LocalSkillIngester(BaseSkillIngester):
    """Ingests skills from a local filesystem directory."""

    def __init__(self, parser: SkillParser | None = None):
        """Initialize the local skill ingester."""
        self.parser = parser or SkillParser()

    async def sync_source(
        self,
        session: AsyncSession,
        source: SkillSource,
        **_kwargs: Any,
    ) -> dict[str, int]:
        """Sync skills from a local directory source."""
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
        for md_file in base_path.rglob("**/SKILL.md"):
            try:
                relative_path = str(md_file.relative_to(Path(source.path)))
                parsed = self.parser.parse_file(md_file)

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
                    stmt_global = select(Skill).where(
                        Skill.category == parsed.category,
                        Skill.slug == parsed.slug,
                    )
                    result_global = await session.execute(stmt_global)
                    existing_global = result_global.scalar_one_or_none()

                    if existing_global:
                        logger.info(
                            "Skipping %s: skill %s/%s already exists from source %s",
                            relative_path,
                            parsed.category,
                            parsed.slug,
                            existing_global.source_id,
                        )
                        stats["unchanged"] += 1
                        continue

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

        source.last_synced_at = datetime.now(UTC)
        source.sync_error = None

        await session.commit()

        return stats


_INGESTER_REGISTRY: dict[str, type[BaseSkillIngester]] = {}


def register_ingester(
    source_type: str,
    ingester_class: type[BaseSkillIngester],
) -> None:
    """Register a skill ingester for a source type."""
    _INGESTER_REGISTRY[source_type] = ingester_class


register_ingester("github", GitHubSkillIngester)
register_ingester("local", LocalSkillIngester)


async def sync_skill_source(
    session: AsyncSession,
    source: SkillSource,
    github_token: str | None = None,
    **kwargs: Any,
) -> dict[str, int]:
    """Sync skills from a source using the registered ingester for its type."""
    source_type = source.source_type

    if source_type not in _INGESTER_REGISTRY:
        registered = ", ".join(_INGESTER_REGISTRY.keys()) or "(none)"
        raise ValueError(
            f"Unknown source type: {source_type}. "
            f"Registered types: {registered}. "
            f"Use register_ingester() to add new types."
        )

    ingester_class = _INGESTER_REGISTRY[source_type]

    # The concrete ingesters have specific init signatures.
    if source_type == "github":
        ingester = ingester_class(github_token=github_token)  # type: ignore[call-arg]
    else:
        ingester = ingester_class()

    try:
        return await ingester.sync_source(session, source, **kwargs)
    finally:
        await ingester.close()
