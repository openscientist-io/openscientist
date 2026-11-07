"""The prompt-formatting turn: records the research question as the display
fragment the job page renders, before discovery starts."""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from openscientist.agent.base import AbstractAgent
from openscientist.database.models.job import Job as JobModel
from openscientist.database.session import async_session_local
from openscientist.formatted_prompt import is_inline_image
from openscientist.orchestrator.iteration import (
    build_prompt_format_prompt,
    build_prompt_format_retry_prompt,
)
from openscientist.providers.base import Provider

logger = logging.getLogger(__name__)

_MAX_PROMPT_FORMAT_ATTEMPTS = 2


async def _read_job(job_id: str) -> JobModel | None:
    async with async_session_local() as session:
        return await session.get(JobModel, UUID(job_id))


async def format_research_prompt(
    executor: AbstractAgent[Provider],
    job_id: str,
    research_question: str,
    data_files: list[str],
) -> None:
    """Have the model record the research question as the job page's display fragment.

    One fresh-session turn and one in-session retry. A turn that fails, raises
    or records nothing ends the step and the job continues to discovery, and the
    page shows the plain question. A job that already has a fragment, as a
    resumed one does, or has no row to hold it gets no turn.
    """
    job = await _read_job(job_id)
    if job is None or job.formatted_prompt:
        return

    image_files = [Path(p).name for p in data_files if is_inline_image(p)]
    other_files = [Path(p).name for p in data_files if not is_inline_image(p)]
    tool_prefix = executor.prompt_fragments().mcp_tool_prefix
    for attempt in range(1, _MAX_PROMPT_FORMAT_ATTEMPTS + 1):
        build = build_prompt_format_prompt if attempt == 1 else build_prompt_format_retry_prompt
        prompt = build(
            research_question,
            image_files=image_files,
            other_files=other_files,
            tool_prefix=tool_prefix,
        )
        try:
            result = await executor.run_iteration(prompt, reset_session=attempt == 1)
        except Exception as e:
            logger.warning("Prompt-format turn %d raised for job %s: %s", attempt, job_id, e)
            return
        job = await _read_job(job_id)
        if job is not None and job.formatted_prompt:
            return
        if not result.success:
            logger.warning(
                "Prompt-format turn %d ended %s for job %s", attempt, result.outcome.value, job_id
            )
            return
        logger.warning("Prompt-format turn %d recorded nothing for job %s", attempt, job_id)
