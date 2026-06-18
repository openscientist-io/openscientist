from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from openscientist.agent.protocol import IterationResult
from openscientist.job_review import (
    REVIEW_REPORT_FILENAME,
    REVIEW_TRANSCRIPT_FILENAME,
    generate_job_review,
)
from openscientist.orchestrator.iteration import build_review_prompt


def test_build_review_prompt_includes_reviewer_structure(tmp_path: Path) -> None:
    prompt = build_review_prompt(
        "Does treatment change metabolism?",
        job_dir=tmp_path,
        final_report_markdown="# Final Report\n\nStrong claim.",
        job_context="# Findings\nF001",
    )

    assert "Does treatment change metabolism?" in prompt
    assert "Major Concerns" in prompt
    assert "Recommendations" in prompt
    assert "final_report_markdown" in prompt
    assert "Do not modify any files" in prompt


@pytest.mark.asyncio
async def test_generate_job_review_writes_report_and_transcript(tmp_path: Path) -> None:
    job_id = "11111111-1111-1111-1111-111111111111"
    job_dir = tmp_path / job_id
    job_dir.mkdir()
    (job_dir / "final_report.md").write_text("# Final Report\n\nAnalysis text.", encoding="utf-8")

    fake_executor = MagicMock()
    fake_executor.run_iteration = AsyncMock(
        return_value=IterationResult(
            success=True,
            output="# Review\n\n## Major Concerns\n- Concern.",
            tool_calls=2,
            transcript=[{"type": "assistant", "message": {"content": []}}],
        )
    )
    fake_executor.shutdown = AsyncMock()

    fake_provider = MagicMock()

    with (
        patch("openscientist.job_review.load_job_context", new_callable=AsyncMock) as load_context,
        patch("openscientist.job_review.get_provider", return_value=fake_provider),
        patch("openscientist.job_review.get_agent_executor", return_value=fake_executor),
    ):
        load_context.return_value = "# Findings\nStructured context"

        result = await generate_job_review(
            job_id=job_id,
            job_dir=job_dir,
            research_question="What happened?",
        )

    review_path = job_dir / REVIEW_REPORT_FILENAME
    transcript_path = job_dir / "provenance" / REVIEW_TRANSCRIPT_FILENAME

    assert result.review_path == review_path
    assert result.transcript_path == transcript_path
    assert result.tool_calls == 2
    assert review_path.read_text(encoding="utf-8").startswith("# Review")
    assert transcript_path.exists()
    fake_provider.setup_environment.assert_called_once()
    fake_executor.run_iteration.assert_awaited_once()
    fake_executor.shutdown.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_job_review_requires_final_report(tmp_path: Path) -> None:
    job_dir = tmp_path / "11111111-1111-1111-1111-111111111111"
    job_dir.mkdir()

    with pytest.raises(FileNotFoundError, match="Final report not found"):
        await generate_job_review(
            job_id=job_dir.name,
            job_dir=job_dir,
            research_question="What happened?",
        )
