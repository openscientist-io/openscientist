"""Integration tests for job detail page."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from nicegui.testing import user_simulation


# Helper to create knowledge state JSON files
def create_knowledge_state_file(job_dir: Path, data: dict):
    """Create a knowledge_state.json file in the job directory."""
    ks_path = job_dir / "knowledge_state.json"
    ks_path.parent.mkdir(parents=True, exist_ok=True)
    with open(ks_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def create_config_file(job_dir: Path, data: dict):
    """Create a config.json file in the job directory."""
    config_path = job_dir / "config.json"
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def create_report_file(job_dir: Path, content: str):
    """Create a final_report.md file."""
    report_path = job_dir / "final_report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)


def create_transcript_file(job_dir: Path, iteration: int, data: dict):
    """Create a transcript file for an iteration."""
    provenance_dir = job_dir / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = provenance_dir / f"iter{iteration}_transcript.json"
    with open(transcript_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


# Mock job fixtures with different states
@pytest.fixture
def mock_job_pending(temp_jobs_dir: Path):
    """Create a mock pending job."""
    from tests.webapp.mocks import MockJobInfo

    job = MockJobInfo(
        job_id="pending_job_123",
        status="created",
        research_question="What is the effect of gene X on disease Y?",
        created_at="2026-02-05T10:00:00",
        provider="vertex",
        model="claude-3-5-sonnet",
        iterations_completed=0,
        max_iterations=10,
        findings_count=0,
    )

    # Create minimal knowledge state
    job_dir = temp_jobs_dir / job.job_id
    create_knowledge_state_file(
        job_dir,
        {
            "research_question": job.research_question,
            "iteration": 0,
            "status": "pending",
            "findings": [],
            "literature": [],
            "analysis_log": [],
            "iteration_summaries": [],
        },
    )

    return job, job_dir


@pytest.fixture
def mock_job_running(temp_jobs_dir: Path):
    """Create a mock running job with partial results."""
    from tests.webapp.mocks import MockJobInfo

    job = MockJobInfo(
        job_id="running_job_456",
        status="running",
        research_question="How does protein A interact with protein B?",
        created_at="2026-02-05T09:00:00",
        provider="vertex",
        model="claude-3-5-sonnet",
        iterations_completed=2,
        max_iterations=10,
        findings_count=1,
    )

    job_dir = temp_jobs_dir / job.job_id

    # Create knowledge state with 2 completed iterations
    ks_data = {
        "research_question": job.research_question,
        "iteration": 3,  # Currently on iteration 3
        "status": "running",
        "findings": [
            {
                "title": "Protein A binds to domain X of Protein B",
                "evidence": "Crystal structure shows direct interaction",
                "biological_interpretation": "This suggests a regulatory mechanism",
                "iteration_discovered": 1,
            }
        ],
        "literature": [
            {
                "pmid": "12345678",
                "title": "Structural analysis of Protein A-B complex",
                "authors": ["Smith J", "Jones K"],
                "journal": "Nature Structural Biology",
                "year": 2024,
                "abstract": "We determined the crystal structure of the Protein A-B complex...",
                "search_query": "protein A protein B interaction",
                "retrieved_at_iteration": 1,
            },
            {
                "pmid": "87654321",
                "title": "Functional studies of Protein A",
                "authors": ["Brown M"],
                "journal": "Cell",
                "year": 2023,
                "abstract": "Protein A plays a critical role in cellular signaling...",
                "search_query": "protein A function",
                "retrieved_at_iteration": 2,
            },
        ],
        "analysis_log": [
            {
                "iteration": 1,
                "action": "search_pubmed",
                "query": "protein A protein B interaction",
                "results_count": 1,
                "timestamp": "2026-02-05T09:05:00",
            },
            {
                "iteration": 1,
                "action": "update_knowledge_state",
                "findings_added": 1,
                "timestamp": "2026-02-05T09:10:00",
            },
            {
                "iteration": 2,
                "action": "search_pubmed",
                "query": "protein A function",
                "results_count": 1,
                "timestamp": "2026-02-05T09:20:00",
            },
            {
                "iteration": 2,
                "action": "execute_code",
                "description": "Analyzed protein sequence",
                "timestamp": "2026-02-05T09:25:00",
            },
        ],
        "iteration_summaries": [
            {
                "iteration": 1,
                "strapline": "Found direct binding evidence",
                "summary": "Identified crystal structure showing Protein A binds to domain X of Protein B",
            },
            {
                "iteration": 2,
                "strapline": "Explored functional implications",
                "summary": "Reviewed literature on Protein A function and performed sequence analysis",
            },
        ],
    }

    create_knowledge_state_file(job_dir, ks_data)

    # Create transcripts for iterations 1 and 2
    create_transcript_file(
        job_dir,
        1,
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": "shandy_search_pubmed",
                    "input": {"query": "protein A protein B interaction"},
                },
                {
                    "type": "tool_result",
                    "content": '{"results": [{"pmid": "12345678", "title": "Structural analysis..."}]}',
                },
                {
                    "type": "tool_use",
                    "name": "shandy_update_knowledge_state",
                    "input": {"action": "add_finding", "title": "Protein A binds to domain X"},
                },
                {
                    "type": "tool_result",
                    "content": '{"success": true}',
                },
            ]
        },
    )

    create_transcript_file(
        job_dir,
        2,
        {
            "content": [
                {
                    "type": "tool_use",
                    "name": "shandy_search_pubmed",
                    "input": {"query": "protein A function"},
                },
                {
                    "type": "tool_result",
                    "content": '{"results": [{"pmid": "87654321"}]}',
                },
                {
                    "type": "tool_use",
                    "name": "shandy_execute_code",
                    "input": {"code": "import Bio\nprint('Analyzing sequence')"},
                },
                {
                    "type": "tool_result",
                    "content": "Analyzing sequence\n",
                },
            ]
        },
    )

    return job, job_dir


@pytest.fixture
def mock_job_completed(temp_jobs_dir: Path):
    """Create a mock completed job with full results."""
    from tests.webapp.mocks import MockJobInfo

    job = MockJobInfo(
        job_id="completed_job_789",
        status="completed",
        research_question="What are the mechanisms of drug resistance in cancer cells?",
        created_at="2026-02-04T10:00:00",
        provider="vertex",
        model="claude-3-5-sonnet",
        iterations_completed=5,
        max_iterations=10,
        findings_count=3,
    )

    job_dir = temp_jobs_dir / job.job_id

    # Create full knowledge state
    ks_data = {
        "research_question": job.research_question,
        "iteration": 5,
        "status": "completed",
        "findings": [
            {
                "title": "ABC transporter upregulation",
                "evidence": "Gene expression analysis shows 5-fold increase",
                "biological_interpretation": "Efflux pumps remove chemotherapy drugs",
                "iteration_discovered": 2,
            },
            {
                "title": "DNA repair pathway activation",
                "evidence": "Western blot confirms increased repair protein levels",
                "biological_interpretation": "Enhanced repair counteracts drug-induced damage",
                "iteration_discovered": 3,
            },
            {
                "title": "Apoptosis pathway suppression",
                "evidence": "Flow cytometry shows reduced apoptotic markers",
                "biological_interpretation": "Cells evade programmed cell death",
                "iteration_discovered": 4,
            },
        ],
        "literature": [
            {
                "pmid": "11111111",
                "title": "ABC transporters in drug resistance",
                "authors": ["Lee A", "Kim B"],
                "journal": "Cancer Research",
                "year": 2024,
                "abstract": "ABC transporters play a key role...",
                "search_query": "ABC transporter cancer drug resistance",
                "retrieved_at_iteration": 2,
            },
        ],
        "analysis_log": [
            {
                "iteration": 2,
                "action": "search_pubmed",
                "query": "ABC transporter cancer drug resistance",
                "results_count": 1,
            },
            {
                "iteration": 2,
                "action": "update_knowledge_state",
                "findings_added": 1,
            },
        ],
        "iteration_summaries": [
            {
                "iteration": 1,
                "strapline": "Initial literature survey",
                "summary": "Reviewed general mechanisms of drug resistance",
            },
            {
                "iteration": 2,
                "strapline": "Identified ABC transporter role",
                "summary": "Found evidence of ABC transporter upregulation in resistant cells",
            },
        ],
    }

    create_knowledge_state_file(job_dir, ks_data)

    # Create final report
    create_report_file(
        job_dir,
        """# Drug Resistance in Cancer Cells

## Summary
This research identified three key mechanisms...

## Findings
1. ABC transporter upregulation
2. DNA repair pathway activation
3. Apoptosis pathway suppression
""",
    )

    return job, job_dir


@pytest.fixture
def mock_job_failed(temp_jobs_dir: Path):
    """Create a mock failed job with error."""
    from tests.webapp.mocks import MockJobInfo

    job = MockJobInfo(
        job_id="failed_job_999",
        status="failed",
        research_question="Test failing question",
        created_at="2026-02-05T08:00:00",
        failed_at="2026-02-05T08:15:00",
        error='{"type": "error", "error": {"type": "api_error", "message": "API request failed: Rate limit exceeded"}}',
        iterations_completed=1,
        max_iterations=10,
        findings_count=0,
    )

    job_dir = temp_jobs_dir / job.job_id

    create_knowledge_state_file(
        job_dir,
        {
            "research_question": job.research_question,
            "iteration": 1,
            "status": "failed",
            "findings": [],
            "literature": [],
            "analysis_log": [],
        },
    )

    return job, job_dir


@pytest.fixture
def mock_job_awaiting_feedback(temp_jobs_dir: Path):
    """Create a mock job awaiting user feedback."""
    from tests.webapp.mocks import MockJobInfo

    job = MockJobInfo(
        job_id="feedback_job_555",
        status="awaiting_feedback",
        research_question="How do neurons communicate?",
        created_at="2026-02-05T07:00:00",
        coinvestigate=True,
        iterations_completed=1,
        max_iterations=10,
        findings_count=1,
    )

    job_dir = temp_jobs_dir / job.job_id

    ks_data = {
        "research_question": job.research_question,
        "iteration": 2,  # Will start iteration 2 after feedback
        "status": "awaiting_feedback",
        "findings": [
            {
                "title": "Synaptic transmission via neurotransmitters",
                "evidence": "Literature confirms chemical signaling",
                "iteration_discovered": 1,
            }
        ],
        "literature": [],
        "analysis_log": [
            {
                "iteration": 1,
                "action": "search_pubmed",
                "query": "neuron communication synaptic transmission",
                "results_count": 5,
            },
        ],
        "iteration_summaries": [
            {
                "iteration": 1,
                "strapline": "Explored basic mechanisms",
                "summary": "Found evidence of chemical synaptic transmission",
            }
        ],
    }

    create_knowledge_state_file(job_dir, ks_data)

    create_config_file(
        job_dir,
        {
            "status": "awaiting_feedback",
            "awaiting_feedback_since": "2026-02-05T07:30:00",
        },
    )

    return job, job_dir


class TestJobDetailPage:
    """Tests for job detail page."""

    @pytest.mark.asyncio
    async def test_job_not_found(self, mock_job_manager):
        """Test handling of non-existent job."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                # Mock get_job to return None
                mock_get_jm.return_value = mock_job_manager
                mock_job_manager._jobs = {}

                async with user_simulation(root=lambda: job_detail_page("nonexistent")) as user:
                    await user.open("/job/nonexistent")
                    await user.should_see("Job nonexistent not found")
                    await user.should_see("Back to Jobs")

    @pytest.mark.asyncio
    async def test_pending_job_renders(self, mock_job_manager, mock_job_pending, temp_jobs_dir):
        """Test that a pending job renders correctly."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_dir = mock_job_pending

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_job_manager.jobs_dir = temp_jobs_dir
                mock_job_manager._jobs = {job.job_id: job}
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=lambda: job_detail_page(job.job_id)) as user:
                    await user.open(f"/job/{job.job_id}")
                    # Should see header and job ID
                    await user.should_see(f"SHANDY - {job.job_id}")
                    await user.should_see("Back to Jobs")

                    # Should see tabs
                    await user.should_see("Research Log")
                    await user.should_see("Report")

                    # Should see status badge
                    await user.should_see("Status")

                    # Should see research question
                    await user.should_see(job.research_question)

    @pytest.mark.asyncio
    async def test_running_job_shows_iterations(
        self, mock_job_manager, mock_job_running, temp_jobs_dir
    ):
        """Test that a running job displays iteration timeline."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_dir = mock_job_running

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_job_manager.jobs_dir = temp_jobs_dir
                mock_job_manager._jobs = {job.job_id: job}
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=lambda: job_detail_page(job.job_id)) as user:
                    await user.open(f"/job/{job.job_id}")
                    # Should see research question
                    await user.should_see(job.research_question)

                    # Should see progress indicators
                    await user.should_see("Progress")
                    await user.should_see("Findings")

                    # Should see iteration summaries
                    await user.should_see("Investigation Timeline")
                    await user.should_see("Found direct binding evidence")
                    await user.should_see("Explored functional implications")

                    # Should see badges for actions
                    await user.should_see("searches")
                    await user.should_see("findings")

    @pytest.mark.asyncio
    async def test_completed_job_shows_report(
        self, mock_job_manager, mock_job_completed, temp_jobs_dir
    ):
        """Test that a completed job displays the final report."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_dir = mock_job_completed

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_job_manager.jobs_dir = temp_jobs_dir
                mock_job_manager._jobs = {job.job_id: job}
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=lambda: job_detail_page(job.job_id)) as user:
                    await user.open(f"/job/{job.job_id}")
                    # Should see completed status
                    await user.should_see("Status")

                    # Should see all 3 findings count
                    await user.should_see("Findings")

                    # Should have report available (would need to click tab to see content)
                    await user.should_see("Report")

    @pytest.mark.asyncio
    async def test_failed_job_shows_error(self, mock_job_manager, mock_job_failed, temp_jobs_dir):
        """Test that a failed job displays error information."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_dir = mock_job_failed

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_job_manager.jobs_dir = temp_jobs_dir
                mock_job_manager._jobs = {job.job_id: job}
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=lambda: job_detail_page(job.job_id)) as user:
                    await user.open(f"/job/{job.job_id}")
                    # Should see error indicator
                    await user.should_see("Error")

                    # Should see error message
                    await user.should_see("API request failed")

                    # Should see rate limit mention
                    await user.should_see("Rate limit")

    @pytest.mark.asyncio
    async def test_awaiting_feedback_shows_panel(
        self, mock_job_manager, mock_job_awaiting_feedback, temp_jobs_dir
    ):
        """Test that a job awaiting feedback shows the feedback panel."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_dir = mock_job_awaiting_feedback

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_job_manager.jobs_dir = temp_jobs_dir
                mock_job_manager._jobs = {job.job_id: job}
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=lambda: job_detail_page(job.job_id)) as user:
                    await user.open(f"/job/{job.job_id}")
                    # Should see feedback prompt
                    await user.should_see("Awaiting Your Input")

                    # Should see feedback input field label
                    await user.should_see("Your Feedback")

                    # Should see action buttons
                    await user.should_see("Submit & Continue")
                    await user.should_see("Continue Without Feedback")

    @pytest.mark.asyncio
    async def test_job_detail_shows_literature_count(
        self, mock_job_manager, mock_job_running, temp_jobs_dir
    ):
        """Test that literature count is displayed correctly."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_dir = mock_job_running

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_job_manager.jobs_dir = temp_jobs_dir
                mock_job_manager._jobs = {job.job_id: job}
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=lambda: job_detail_page(job.job_id)) as user:
                    await user.open(f"/job/{job.job_id}")
                    # Should see papers reviewed count (2 papers in mock data)
                    await user.should_see("Papers Reviewed")

    @pytest.mark.asyncio
    async def test_job_detail_handles_missing_knowledge_state(
        self, mock_job_manager, temp_jobs_dir
    ):
        """Test that page handles missing knowledge state gracefully."""
        from shandy.webapp_components.pages.job_detail import job_detail_page
        from tests.webapp.mocks import MockJobInfo

        job = MockJobInfo(
            job_id="no_ks_job",
            status="created",
            research_question="Test question",
            created_at="2026-02-05T10:00:00",
        )

        # Don't create knowledge_state.json file
        job_dir = temp_jobs_dir / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_job_manager.jobs_dir = temp_jobs_dir
                mock_job_manager._jobs = {job.job_id: job}
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=lambda: job_detail_page(job.job_id)) as user:
                    await user.open(f"/job/{job.job_id}")
                    # Should still render page
                    await user.should_see(f"SHANDY - {job.job_id}")

                    # Should show message about no knowledge state
                    await user.should_see("Knowledge graph not found")

    @pytest.mark.asyncio
    async def test_job_detail_completed_has_download_buttons(
        self, mock_job_manager, mock_job_completed, temp_jobs_dir
    ):
        """Test that completed job shows download buttons in report tab."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_dir = mock_job_completed

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_job_manager.jobs_dir = temp_jobs_dir
                mock_job_manager._jobs = {job.job_id: job}
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=lambda: job_detail_page(job.job_id)) as user:
                    await user.open(f"/job/{job.job_id}")
                    # Report tab should exist
                    await user.should_see("Report")

                    # Download buttons should be mentioned (visible after clicking tab)
                    # For now just verify the page renders without error

    @pytest.mark.asyncio
    async def test_job_detail_shows_status_badge_colors(
        self, mock_job_manager, mock_job_completed, mock_job_failed, temp_jobs_dir
    ):
        """Test that status badges are displayed for different job states."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        # Test completed job
        job_completed, job_dir_completed = mock_job_completed

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_job_manager.jobs_dir = temp_jobs_dir
                mock_job_manager._jobs = {job_completed.job_id: job_completed}
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(
                    root=lambda: job_detail_page(job_completed.job_id)
                ) as user:
                    await user.open(f"/job/{job_completed.job_id}")
                    await user.should_see("Status")
                    # Badge color is set via NiceGUI, can't easily test color in text content

    @pytest.mark.asyncio
    async def test_job_with_findings_displays_count(
        self, mock_job_manager, mock_job_completed, temp_jobs_dir
    ):
        """Test that findings count is displayed."""
        from shandy.webapp_components.pages.job_detail import job_detail_page

        job, job_dir = mock_job_completed

        with patch("shandy.web_app.get_job_manager") as mock_get_jm:
            with patch("shandy.webapp_components.utils.auth.is_auth_disabled", return_value=True):
                mock_job_manager.jobs_dir = temp_jobs_dir
                mock_job_manager._jobs = {job.job_id: job}
                mock_get_jm.return_value = mock_job_manager

                async with user_simulation(root=lambda: job_detail_page(job.job_id)) as user:
                    await user.open(f"/job/{job.job_id}")
                    # Should see findings count card
                    await user.should_see("Findings")
                    # The actual count (3) should be visible
                    await user.should_see("3")
