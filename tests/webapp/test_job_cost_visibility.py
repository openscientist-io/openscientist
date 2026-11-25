from datetime import UTC, datetime
from uuid import uuid4

import pytest

from openscientist.database.models import Job
from openscientist.job.types import JobInfo
from openscientist.webapp_components.pages.job_detail.timeline import _stats_badges
from openscientist.webapp_components.pages.jobs_list import _owned_rows


@pytest.mark.parametrize("status,field", [("queued", "queue_reason"), ("completed", "stop_note")])
def test_hidden_cost_badges_remove_amounts(status: str, field: str) -> None:
    job = Job(owner_id=uuid4(), research_question="Question", status=status)
    job.id = uuid4()
    job.current_iteration = 0
    job.max_iterations = 5
    job.created_at = datetime.now(UTC)
    job.updated_at = job.created_at
    setattr(job, field, "Job cost limit of $10.00 reached: the job has spent $12.00.")
    info = JobInfo.from_db_model(job, 0, 0)
    badges = _stats_badges(info, 0, spend=12.0, show_costs=False)
    assert not any(label == "Spend" for label, _, _ in badges)
    assert "$" not in str(badges)
    assert "The job reached its cost limit." in str(badges)


def test_hidden_owned_rows_omit_cost_data() -> None:
    job = Job(owner_id=uuid4(), research_question="Question", status="completed")
    job.id = uuid4()
    job.created_at = datetime.now(UTC)
    job.current_iteration = 0
    job.max_iterations = 5
    rows = _owned_rows([job], {}, {job.id: 12.0}, show_costs=False)
    assert "cost" not in rows[0]
