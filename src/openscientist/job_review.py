"""Generate independent reviewer reports for completed OpenScientist jobs."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from openscientist.agent.factory import get_agent_executor
from openscientist.job_chat import load_job_context
from openscientist.orchestrator.discovery import _save_transcript
from openscientist.orchestrator.iteration import build_review_prompt
from openscientist.providers import get_provider

logger = logging.getLogger(__name__)

REVIEW_REPORT_FILENAME = "reviewer_report.md"
REVIEW_TRANSCRIPT_FILENAME = "reviewer_transcript.json"


@dataclass(frozen=True)
class ReviewGenerationResult:
    """Result metadata for a generated reviewer report."""

    review_path: Path
    transcript_path: Path
    tool_calls: int


def review_report_path(job_dir: Path) -> Path:
    """Return the canonical reviewer report path for a job directory."""
    return Path(job_dir) / REVIEW_REPORT_FILENAME


def review_transcript_path(job_dir: Path) -> Path:
    """Return the canonical reviewer transcript path for a job directory."""
    return Path(job_dir) / "provenance" / REVIEW_TRANSCRIPT_FILENAME


def _read_reviewer_claude_md_template() -> str:
    """Read the packaged CLAUDE.md template used by the reviewer agent."""
    return (
        resources.files("openscientist.templates")
        .joinpath("REVIEWER_CLAUDE.md")
        .read_text(encoding="utf-8")
    )


def write_reviewer_claude_md(job_dir: Path) -> None:
    """Write reviewer-specific CLAUDE.md instructions into the job directory."""
    claude_dir = Path(job_dir) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    dest = claude_dir / "CLAUDE.md"
    dest.write_text(_read_reviewer_claude_md_template(), encoding="utf-8")
    logger.debug("Wrote reviewer CLAUDE.md to %s", dest)


def _read_final_report(job_dir: Path) -> str:
    report_path = Path(job_dir) / "final_report.md"
    if not report_path.exists():
        raise FileNotFoundError(f"Final report not found at {report_path}")
    return report_path.read_text(encoding="utf-8")


async def generate_job_review(
    *,
    job_id: str,
    job_dir: Path,
    research_question: str,
) -> ReviewGenerationResult:
    """Generate ``reviewer_report.md`` for a completed job.

    The reviewer is a short-lived agent run with read-oriented instructions.
    Its Markdown response is persisted by the application rather than by the
    agent so the artifact path is deterministic.
    """
    job_dir = Path(job_dir)
    final_report_markdown = _read_final_report(job_dir)
    job_context = await load_job_context(job_id)
    prompt = build_review_prompt(
        research_question,
        job_dir=job_dir,
        final_report_markdown=final_report_markdown,
        job_context=job_context,
    )

    provider = get_provider()
    provider.setup_environment()
    write_reviewer_claude_md(job_dir)

    executor = get_agent_executor(
        job_dir=job_dir,
        data_file=None,
        system_prompt=(
            "You are an independent scientific reviewer for completed OpenScientist analyses. "
            "Evaluate rigor, evidence, reproducibility, and overclaiming. Be specific and fair."
        ),
    )

    try:
        result = await executor.run_iteration(prompt, reset_session=True)
        if not result.success:
            raise RuntimeError(result.error or "Reviewer agent failed")
        review_markdown = result.output.strip()
        if not review_markdown:
            raise RuntimeError("Reviewer agent returned no output")

        output_path = review_report_path(job_dir)
        output_path.write_text(f"{review_markdown}\n", encoding="utf-8")

        transcript_path = review_transcript_path(job_dir)
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        _save_transcript(transcript_path, result.transcript)

        logger.info(
            "Generated reviewer report for job %s at %s (%d tool calls)",
            job_id,
            output_path,
            result.tool_calls,
        )
        return ReviewGenerationResult(
            review_path=output_path,
            transcript_path=transcript_path,
            tool_calls=result.tool_calls,
        )
    finally:
        await executor.shutdown()


def read_review_metadata(job_dir: Path) -> dict[str, object]:
    """Return lightweight metadata about an existing reviewer report."""
    path = review_report_path(job_dir)
    if not path.exists():
        return {"available": False}
    return {
        "available": True,
        "filename": path.name,
        "size_bytes": path.stat().st_size,
    }
