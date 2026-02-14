"""
Tests for skill ingestion service.

Tests SkillParser for markdown parsing, GitHubSkillIngester for API interactions
(with mocking), and LocalSkillIngester.
"""

import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shandy.database.models import Skill, SkillSource
from shandy.skill_ingestion import (
    GitHubSkillIngester,
    LocalSkillIngester,
    SkillParseError,
    SkillParser,
)


class TestSkillParser:
    """Tests for SkillParser."""

    def test_parse_valid_skill(self, sample_skill_markdown: str):
        """Test parsing a valid skill markdown file."""
        parser = SkillParser()
        parsed = parser.parse_content(sample_skill_markdown, "test/skill.md")

        assert parsed.name == "Test Skill"
        assert parsed.category == "testing"
        assert parsed.description == "A skill for testing purposes"
        assert parsed.tags == ["test", "example"]
        assert "# Test Skill" in parsed.content
        assert parsed.slug == "skill"  # Derived from filename
        assert len(parsed.content_hash) == 64  # SHA256 hex

    def test_parse_missing_frontmatter(self):
        """Test parsing fails without frontmatter."""
        parser = SkillParser()
        content = "# Just Markdown\n\nNo frontmatter here."

        with pytest.raises(SkillParseError, match="Missing or malformed YAML frontmatter"):
            parser.parse_content(content, "test.md")

    def test_parse_missing_name(self):
        """Test parsing fails without name field."""
        parser = SkillParser()
        content = """---
category: testing
---

# Content
"""

        with pytest.raises(SkillParseError, match="Missing 'name'"):
            parser.parse_content(content, "test.md")

    def test_parse_missing_category(self):
        """Test parsing fails without category field."""
        parser = SkillParser()
        content = """---
name: Test Skill
---

# Content
"""

        with pytest.raises(SkillParseError, match="Missing 'category'"):
            parser.parse_content(content, "test.md")

    def test_parse_invalid_yaml(self):
        """Test parsing fails with invalid YAML."""
        parser = SkillParser()
        content = """---
name: Test Skill
category: [invalid yaml
---

# Content
"""

        with pytest.raises(SkillParseError, match="Invalid YAML"):
            parser.parse_content(content, "test.md")

    def test_parse_custom_slug(self):
        """Test parsing with custom slug in frontmatter."""
        parser = SkillParser()
        content = """---
name: Test Skill
category: testing
slug: custom-slug
---

# Content
"""
        parsed = parser.parse_content(content, "different-name.md")
        assert parsed.slug == "custom-slug"

    def test_parse_file(self, tmp_path: Path, sample_skill_markdown: str):
        """Test parsing from file."""
        skill_file = tmp_path / "test-skill.md"
        skill_file.write_text(sample_skill_markdown)

        parser = SkillParser()
        parsed = parser.parse_file(skill_file)

        assert parsed.name == "Test Skill"
        assert parsed.slug == "test-skill"

    def test_content_hash_deterministic(self):
        """Test that content hash is deterministic."""
        parser = SkillParser()
        content = """---
name: Test
category: test
---

Content
"""
        parsed1 = parser.parse_content(content, "test.md")
        parsed2 = parser.parse_content(content, "test.md")

        assert parsed1.content_hash == parsed2.content_hash

    def test_content_hash_changes_with_content(self):
        """Test that content hash changes when content changes."""
        parser = SkillParser()
        content1 = """---
name: Test
category: test
---

Content 1
"""
        content2 = """---
name: Test
category: test
---

Content 2
"""
        parsed1 = parser.parse_content(content1, "test.md")
        parsed2 = parser.parse_content(content2, "test.md")

        assert parsed1.content_hash != parsed2.content_hash


class TestGitHubSkillIngester:
    """Tests for GitHubSkillIngester with mocked HTTP."""

    def test_parse_github_url(self):
        """Test parsing GitHub URLs."""
        ingester = GitHubSkillIngester()

        # Standard URL
        owner, repo = ingester._parse_github_url("https://github.com/owner/repo")
        assert owner == "owner"
        assert repo == "repo"

        # URL with .git suffix
        owner, repo = ingester._parse_github_url("https://github.com/owner/repo.git")
        assert owner == "owner"
        assert repo == "repo"

        # URL with trailing slash
        owner, repo = ingester._parse_github_url("https://github.com/owner/repo/")
        assert owner == "owner"
        assert repo == "repo"

    def test_parse_invalid_github_url(self):
        """Test parsing invalid GitHub URLs."""
        ingester = GitHubSkillIngester()

        with pytest.raises(ValueError, match="Invalid GitHub URL"):
            ingester._parse_github_url("https://gitlab.com/owner/repo")

        with pytest.raises(ValueError, match="Invalid GitHub URL"):
            ingester._parse_github_url("not-a-url")

    @pytest.mark.asyncio
    async def test_get_latest_commit(self):
        """Test getting latest commit SHA."""
        ingester = GitHubSkillIngester()

        with patch.object(ingester, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {"sha": "abc123def456"}
            mock_response.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            sha = await ingester.get_latest_commit("owner", "repo", "main")

            assert sha == "abc123def456"
            mock_client.get.assert_called_once()

        await ingester.close()

    @pytest.mark.asyncio
    async def test_list_skill_files(self):
        """Test listing skill files from a repo."""
        ingester = GitHubSkillIngester()

        mock_tree = {
            "tree": [
                {"path": "skills/analysis.md", "type": "blob", "sha": "sha1"},
                {"path": "skills/pipeline.md", "type": "blob", "sha": "sha2"},
                {"path": "skills/readme.txt", "type": "blob", "sha": "sha3"},  # Not .md
                {"path": "other/file.md", "type": "blob", "sha": "sha4"},  # Wrong path
                {"path": "skills", "type": "tree", "sha": "sha5"},  # Not a blob
            ]
        }

        with patch.object(ingester, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = mock_tree
            mock_response.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            files = await ingester.list_skill_files("owner", "repo", "main", "skills")

            assert len(files) == 2
            assert files[0]["path"] == "skills/analysis.md"
            assert files[1]["path"] == "skills/pipeline.md"

        await ingester.close()


class TestLocalSkillIngester:
    """Tests for LocalSkillIngester."""

    @pytest.mark.asyncio
    async def test_sync_local_source(
        self,
        db_session: AsyncSession,
        sample_skill_markdown: str,
    ):
        """Test syncing skills from local directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create skill files
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir()

            (skills_dir / "test-skill.md").write_text(sample_skill_markdown)

            # Create source
            source = SkillSource(
                name="Local Test",
                source_type="local",
                path=tmpdir,
                skills_path="skills",
                is_enabled=True,
            )
            db_session.add(source)
            await db_session.commit()
            await db_session.refresh(source)

            # Sync
            ingester = LocalSkillIngester()
            stats = await ingester.sync_source(db_session, source)

            assert stats["created"] == 1
            assert stats["updated"] == 0
            assert stats["unchanged"] == 0
            assert stats["errors"] == 0

            # Verify skill was created
            stmt = select(Skill).where(Skill.source_id == source.id)
            result = await db_session.execute(stmt)
            skills = list(result.scalars().all())

            assert len(skills) == 1
            assert skills[0].name == "Test Skill"
            assert skills[0].category == "testing"

    @pytest.mark.asyncio
    async def test_sync_local_source_update(
        self,
        db_session: AsyncSession,
        sample_skill_markdown: str,
    ):
        """Test updating skills on re-sync."""
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_dir = Path(tmpdir) / "skills"
            skills_dir.mkdir()
            skill_file = skills_dir / "test-skill.md"
            skill_file.write_text(sample_skill_markdown)

            # Create source
            source = SkillSource(
                name="Local Test",
                source_type="local",
                path=tmpdir,
                skills_path="skills",
                is_enabled=True,
            )
            db_session.add(source)
            await db_session.commit()
            await db_session.refresh(source)

            # Initial sync
            ingester = LocalSkillIngester()
            stats1 = await ingester.sync_source(db_session, source)
            assert stats1["created"] == 1

            # Sync again without changes
            stats2 = await ingester.sync_source(db_session, source)
            assert stats2["unchanged"] == 1

            # Modify file and sync
            new_content = sample_skill_markdown.replace(
                "This is the skill content.",
                "Updated content here.",
            )
            skill_file.write_text(new_content)

            stats3 = await ingester.sync_source(db_session, source)
            assert stats3["updated"] == 1

    @pytest.mark.asyncio
    async def test_sync_invalid_source_type(self, db_session: AsyncSession):
        """Test that syncing with wrong source type raises error."""
        source = SkillSource(
            name="GitHub Source",
            source_type="github",  # Wrong type for LocalSkillIngester
            url="https://github.com/test/repo",
            is_enabled=True,
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        ingester = LocalSkillIngester()

        with pytest.raises(ValueError, match="not a local source"):
            await ingester.sync_source(db_session, source)
