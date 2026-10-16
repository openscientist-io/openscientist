"""Tests for how the job timeline reports an iteration's activity."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from openscientist.job.types import JobStatus
from openscientist.webapp_components.pages import job_detail


def _entries(*actions: str) -> list[Any]:
    return [{"action": action} for action in actions]


def test_activity_counts_split_by_action() -> None:
    """Each action is counted under its own heading, delegations included."""
    counts = job_detail._iteration_activity_counts(
        _entries(
            "execute_code",
            "execute_code",
            "search_pubmed",
            "update_knowledge_state",
            "delegate_to_expert",
            "delegate_to_expert",
            "delegate_to_expert",
            "something_else",
        )
    )

    assert counts == (2, 1, 1, 3)


def test_an_iteration_that_only_delegated_is_not_reported_as_idle() -> None:
    """Delegation is work, so the row must not fall back to the grey border."""
    border = job_detail._timeline_border_class(0, 0, 0, 1)

    assert border == "border-l-4 border-teal-300"
    assert border != job_detail._timeline_border_class(0, 0, 0, 0)


def test_a_finding_still_outranks_a_delegation() -> None:
    """The border shows the strongest outcome, and a finding is the strongest."""
    assert job_detail._timeline_border_class(1, 1, 1, 1) == "border-l-4 border-green-500"
    assert job_detail._timeline_border_class(1, 0, 0, 1) == "border-l-4 border-blue-300"


def test_a_delegation_action_card_is_distinguishable() -> None:
    """A delegation entry gets its own card colour, not the unknown-tool grey."""
    assert job_detail._action_card_class("delegate_to_expert") == (
        "w-full mb-2 border-l-4 border-teal-300"
    )
    assert job_detail._action_card_class("mystery_tool") == "w-full mb-2 border-l-4 border-gray-300"


def test_a_delegating_iteration_shows_a_delegation_badge() -> None:
    """The header tells the scientist how many experts the turn used."""
    with patch.object(job_detail, "ui", MagicMock()) as ui:
        job_detail._render_iteration_header(2, "header", 0, 0, 0, expert_count=3)

    assert any("3 delegations" in str(call.args) for call in ui.badge.call_args_list)


def test_a_non_delegating_iteration_shows_no_delegation_badge() -> None:
    """No delegations means no badge, rather than a badge reading zero."""
    with patch.object(job_detail, "ui", MagicMock()) as ui:
        job_detail._render_iteration_header(2, "header", 1, 0, 0)

    assert not any("delegations" in str(call.args) for call in ui.badge.call_args_list)


def test_a_delegating_iteration_card_carries_the_delegation_border(tmp_path: Path) -> None:
    """The whole card, not just the header, reflects a delegation-only turn."""
    with patch.object(job_detail, "ui", MagicMock()) as ui:
        job_detail._render_iteration_card(
            1,
            _entries("delegate_to_expert"),
            {"strapline": "delegated the search"},
            {"hypotheses": []},
            1,
            JobStatus.COMPLETED,
            tmp_path,
        )

    classes = ui.expansion.return_value.classes
    assert "border-teal-300" in str(classes.call_args.args)
