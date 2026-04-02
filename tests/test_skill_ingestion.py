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

from openscientist.database.models import Skill, SkillSource
from openscientist.skill_ingestion import (
    GitHubSkillIngester,
    LocalSkillIngester,
    SkillParseError,
    SkillParser,
)

# Resolved path to the real skills/ directory at project root
BUILTIN_SKILLS_DIR = Path(__file__).parent.parent / "skills"


class TestSkillParser:
    """Tests for SkillParser."""

    def test_parse_valid_skill(self, sample_skill_markdown: str):
        """Test parsing a valid skill markdown file."""
        parser = SkillParser()
        parsed = parser.parse_content(sample_skill_markdown, "skills/testing/test-skill.md")

        assert parsed.name == "Test Skill"
        assert parsed.category == "testing"
        assert parsed.description == "A skill for testing purposes"
        assert parsed.tags == ["test", "example"]
        assert "# Test Skill" in parsed.content
        assert parsed.slug == "test-skill"  # Derived from filename
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

    def test_parse_missing_category_no_path(self):
        """Test parsing fails without category when path has no parent directory."""
        parser = SkillParser()
        content = """---
name: Test Skill
---

# Content
"""
        # Single-component path can't derive category from parent directory
        with pytest.raises(SkillParseError, match="cannot derive from path"):
            parser.parse_content(content, "test.md")

    def test_derive_category_from_path(self):
        """Test category is derived from parent directory when not in frontmatter."""
        parser = SkillParser()
        content = """---
name: BioPython Analysis
---

# BioPython Analysis

Skill content here.
"""
        # Path: scientific-skills/biopython/SKILL.md -> category = "biopython"
        parsed = parser.parse_content(content, "scientific-skills/biopython/SKILL.md")
        assert parsed.category == "biopython"

    def test_derive_slug_from_directory_for_skill_md(self):
        """Test slug is derived from parent directory for SKILL.md files."""
        parser = SkillParser()
        content = """---
name: PubChem Tools
category: pubchem
---

# PubChem Tools

Skill content.
"""
        # For SKILL.md files, slug should come from parent directory
        parsed = parser.parse_content(content, "scientific-skills/pubchem/SKILL.md")
        assert parsed.slug == "pubchem"

        # For regular filenames, slug should come from filename
        parsed2 = parser.parse_content(content, "scientific-skills/pubchem/analysis.md")
        assert parsed2.slug == "analysis"

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
        """Test listing skill files from a repo (only SKILL.md files)."""
        ingester = GitHubSkillIngester()

        # Simulate K-Dense-style repo structure where each skill is in
        # its own directory with a SKILL.md file
        mock_tree = {
            "tree": [
                # Valid SKILL.md files in skill directories
                {
                    "path": "scientific-skills/biopython/SKILL.md",
                    "type": "blob",
                    "sha": "sha1",
                },
                {
                    "path": "scientific-skills/pubchem/SKILL.md",
                    "type": "blob",
                    "sha": "sha2",
                },
                # Reference files that should be filtered out
                {
                    "path": "scientific-skills/biopython/examples.md",
                    "type": "blob",
                    "sha": "sha3",
                },
                {
                    "path": "scientific-skills/README.md",
                    "type": "blob",
                    "sha": "sha4",
                },
                # Wrong directory
                {"path": "other/tool/SKILL.md", "type": "blob", "sha": "sha5"},
                # Directory entry, not a blob
                {"path": "scientific-skills", "type": "tree", "sha": "sha6"},
            ]
        }

        with patch.object(ingester, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = mock_tree
            mock_response.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            files = await ingester.list_skill_files("owner", "repo", "main", "scientific-skills")

            # Should only include SKILL.md files in the correct path
            assert len(files) == 2
            assert files[0]["path"] == "scientific-skills/biopython/SKILL.md"
            assert files[1]["path"] == "scientific-skills/pubchem/SKILL.md"

        await ingester.close()

    @pytest.mark.asyncio
    async def test_github_ingester_end_to_end_with_kdense_format(
        self,
        db_session: AsyncSession,
    ):
        """Test end-to-end GitHub skill ingestion with K-Dense format.

        Verifies that:
        - Mocked GitHub API returns tree with SKILL.md files
        - Parser correctly derives category and slug from directory structure
        - Skills are created in database with correct fields
        - Reference files (non-SKILL.md) are filtered out
        """
        ingester = GitHubSkillIngester()

        # Mock tree response (simulating K-Dense repo structure)
        mock_tree = {
            "tree": [
                {
                    "path": "scientific-skills/biopython/SKILL.md",
                    "type": "blob",
                    "sha": "sha1",
                },
                {
                    "path": "scientific-skills/pubchem/SKILL.md",
                    "type": "blob",
                    "sha": "sha2",
                },
                # Reference file that should be filtered out
                {
                    "path": "scientific-skills/biopython/examples.md",
                    "type": "blob",
                    "sha": "sha3",
                },
            ]
        }

        # Mock skill file contents (K-Dense format: no category in frontmatter)
        biopython_content = """---
name: BioPython Analysis
description: Analyze biological sequences with BioPython
tags:
  - biology
  - sequences
---

# BioPython Analysis

Use BioPython for sequence analysis.
"""
        pubchem_content = """---
name: PubChem Tools
description: Query chemical compound data from PubChem
tags:
  - chemistry
  - compounds
---

# PubChem Tools

Access PubChem compound data.
"""

        def mock_get_response(url, **_kwargs):
            """Return appropriate mock response based on URL."""
            response = MagicMock()
            response.raise_for_status = MagicMock()

            if "commits" in url:
                response.json.return_value = {"sha": "abc123def456"}
            elif "trees" in url:
                response.json.return_value = mock_tree
            elif "biopython/SKILL.md" in url:
                response.text = biopython_content
            elif "pubchem/SKILL.md" in url:
                response.text = pubchem_content
            else:
                response.text = ""

            return response

        # Create source
        source = SkillSource(
            name="K-Dense Scientific Skills",
            source_type="github",
            url="https://github.com/K-Dense-AI/claude-scientific-skills",
            branch="main",
            skills_path="scientific-skills",
            is_enabled=True,
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        with patch.object(ingester, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.side_effect = mock_get_response
            mock_get_client.return_value = mock_client

            stats = await ingester.sync_source(db_session, source)

            # Verify stats
            assert stats["created"] == 2
            assert stats["errors"] == 0

            # Verify skills were created with correct fields
            stmt = select(Skill).where(Skill.source_id == source.id)
            result = await db_session.execute(stmt)
            skills = {s.slug: s for s in result.scalars().all()}

            assert len(skills) == 2

            # Check biopython skill
            assert "biopython" in skills
            biopython = skills["biopython"]
            assert biopython.name == "BioPython Analysis"
            assert biopython.category == "biopython"  # Derived from directory
            assert biopython.description == "Analyze biological sequences with BioPython"
            assert "biology" in biopython.tags

            # Check pubchem skill
            assert "pubchem" in skills
            pubchem = skills["pubchem"]
            assert pubchem.name == "PubChem Tools"
            assert pubchem.category == "pubchem"  # Derived from directory
            assert pubchem.description == "Query chemical compound data from PubChem"

        await ingester.close()

    @pytest.mark.asyncio
    async def test_sync_skips_when_commit_sha_unchanged(
        self,
        db_session: AsyncSession,
    ):
        """Test that sync short-circuits when commit SHA is unchanged."""
        ingester = GitHubSkillIngester()

        source = SkillSource(
            name="Cached Source",
            source_type="github",
            url="https://github.com/owner/repo",
            branch="main",
            skills_path="skills",
            is_enabled=True,
            last_commit_sha="abc123def456",
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        with patch.object(ingester, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_response = MagicMock()
            mock_response.json.return_value = {"sha": "abc123def456"}
            mock_response.raise_for_status = MagicMock()
            mock_client.get.return_value = mock_response
            mock_get_client.return_value = mock_client

            stats = await ingester.sync_source(db_session, source)

            assert stats["skipped_reason"] == "commit_unchanged"
            assert stats["created"] == 0
            assert stats["updated"] == 0
            # Only 1 API call (commit check), no tree listing or file downloads
            assert mock_client.get.call_count == 1

        await ingester.close()

    @pytest.mark.asyncio
    async def test_sync_force_bypasses_sha_check(
        self,
        db_session: AsyncSession,
    ):
        """Test that force=True bypasses the commit SHA short-circuit."""
        ingester = GitHubSkillIngester()

        source = SkillSource(
            name="Forced Source",
            source_type="github",
            url="https://github.com/owner/repo",
            branch="main",
            skills_path="skills",
            is_enabled=True,
            last_commit_sha="abc123def456",
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        mock_tree: dict[str, list[str]] = {"tree": []}  # Empty tree, no skill files

        def mock_get_response(url, **_kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if "commits" in url:
                response.json.return_value = {"sha": "abc123def456"}
            elif "trees" in url:
                response.json.return_value = mock_tree
            return response

        with patch.object(ingester, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.side_effect = mock_get_response
            mock_get_client.return_value = mock_client

            stats = await ingester.sync_source(db_session, source, force=True)

            assert "skipped_reason" not in stats
            # 2 API calls: commit check + tree listing
            assert mock_client.get.call_count == 2

        await ingester.close()

    @pytest.mark.asyncio
    async def test_sync_proceeds_when_commit_sha_changed(
        self,
        db_session: AsyncSession,
    ):
        """Test that sync proceeds when commit SHA differs from stored."""
        ingester = GitHubSkillIngester()

        source = SkillSource(
            name="Changed Source",
            source_type="github",
            url="https://github.com/owner/repo",
            branch="main",
            skills_path="skills",
            is_enabled=True,
            last_commit_sha="old_sha_000",
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        mock_tree: dict[str, list[str]] = {"tree": []}  # Empty tree, no skill files

        def mock_get_response(url, **_kwargs):
            response = MagicMock()
            response.raise_for_status = MagicMock()
            if "commits" in url:
                response.json.return_value = {"sha": "new_sha_999"}
            elif "trees" in url:
                response.json.return_value = mock_tree
            return response

        with patch.object(ingester, "_get_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.get.side_effect = mock_get_response
            mock_get_client.return_value = mock_client

            stats = await ingester.sync_source(db_session, source)

            assert "skipped_reason" not in stats
            # 2 API calls: commit check + tree listing
            assert mock_client.get.call_count == 2

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
            # Create skill files (must be SKILL.md in subdirectory)
            skills_dir = Path(tmpdir) / "skills"
            skill_subdir = skills_dir / "testing"
            skill_subdir.mkdir(parents=True)

            (skill_subdir / "SKILL.md").write_text(sample_skill_markdown)

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
            skill_subdir = skills_dir / "testing"
            skill_subdir.mkdir(parents=True)
            skill_file = skill_subdir / "SKILL.md"
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


class TestBuiltinSkillsIngestion:
    """Integration tests for loading the real built-in skills from skills/."""

    @pytest.mark.asyncio
    async def test_builtin_skills_load(self, db_session: AsyncSession):
        """Test that all built-in SKILL.md files ingest successfully."""
        assert BUILTIN_SKILLS_DIR.is_dir(), f"skills dir not found: {BUILTIN_SKILLS_DIR}"

        source = SkillSource(
            name="Built-in Skills Integration Test",
            source_type="local",
            path=str(BUILTIN_SKILLS_DIR),
            is_enabled=True,
        )
        db_session.add(source)
        await db_session.commit()
        await db_session.refresh(source)

        ingester = LocalSkillIngester()
        stats = await ingester.sync_source(db_session, source)

        assert stats["errors"] == 0
        assert stats["created"] == 10

        # Verify all expected slugs are present
        stmt = select(Skill).where(Skill.source_id == source.id)
        result = await db_session.execute(stmt)
        skills = {s.slug: s for s in result.scalars().all()}

        expected_slugs = {
            "data-science",
            "genomics",
            "jgi-lakehouse",
            "kbase-query",
            "metabolomics",
            "phenix-tools-reference",
            "hypothesis-generation",
            "prioritization",
            "result-interpretation",
            "stopping-criteria",
        }
        assert set(skills.keys()) == expected_slugs

        # Verify 6 domain + 4 workflow category split
        domain_skills = [s for s in skills.values() if s.category == "domain"]
        workflow_skills = [s for s in skills.values() if s.category == "workflow"]
        assert len(domain_skills) == 6
        assert len(workflow_skills) == 4

        # Spot-check one skill's metadata
        genomics = skills["genomics"]
        assert genomics.name == "genomics"
        assert genomics.description == "Genomics and transcriptomics analysis strategies"

        phenix_reference = skills["phenix-tools-reference"]
        assert phenix_reference.name == "phenix-tools-reference"
        assert (
            phenix_reference.description
            == "Reference of available Phenix commands for structural biology analysis"
        )
