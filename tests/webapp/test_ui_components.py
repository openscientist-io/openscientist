"""Tests for UI components module."""

import contextlib
import html
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

from openscientist.job_manager import JobManager, JobStatus
from openscientist.webapp_components.ui_components import (
    OPENSCIENTIST_GITHUB_URL,
    OPENSCIENTIST_PAPER_URL,
    OPENSCIENTIST_RELEASE_URL,
    STATUS_COLORS,
    _get_job_id_badge_html,
    _get_pubmed_badge_html,
    _inject_thinking_status_styles,
    get_project_resource_links,
    register_badge_head_html,
    render_job_id_badge,
    render_job_id_slot,
    render_pmid_badge,
    render_status_cell_slot,
    render_text_with_pmid_links,
    render_thinking_status,
    transform_pmid_references,
)


class TestStatusConstants:
    """Tests for the status color mapping."""

    def test_status_colors_defined(self):
        """Test that all job statuses have color mappings."""
        for status in JobStatus:
            assert status in STATUS_COLORS
            assert isinstance(STATUS_COLORS[status], str)


class TestProjectResourceLinks:
    """Tests for shared public resource links."""

    def test_links_expose_repo_paper_and_release(self):
        """Resource strip should expose only links reachable without auth."""
        assert OPENSCIENTIST_PAPER_URL is not None
        assert get_project_resource_links() == [
            ("GitHub", OPENSCIENTIST_GITHUB_URL),
            ("Paper", OPENSCIENTIST_PAPER_URL),
            ("Latest Release", OPENSCIENTIST_RELEASE_URL),
        ]


class TestRenderStatusCellSlot:
    """Tests for render_status_cell_slot function."""

    def test_returns_string(self):
        """Test that function returns a string template."""
        template = render_status_cell_slot()
        assert isinstance(template, str)

    def test_contains_quasar_elements(self):
        """Test that template contains Quasar components."""
        template = render_status_cell_slot()
        assert "<q-td" in template
        assert "<q-badge" in template
        assert "props.row.status" in template

    def test_contains_all_status_conditions(self):
        """Test that template includes all status types."""
        template = render_status_cell_slot()

        # Check for all status conditions
        assert "props.row.status === 'failed'" in template
        assert "props.row.status === 'completed'" in template
        assert "props.row.status === 'running'" in template
        assert "props.row.status === 'queued'" in template
        assert "props.row.status === 'awaiting_feedback'" in template
        assert "props.row.status === 'cancelled'" in template

    def test_contains_status_icons(self):
        """Test that template includes status icons."""
        template = render_status_cell_slot()
        assert "✗" in template  # Failed
        assert "✓" in template  # Completed
        assert "▶" in template  # Running
        assert "⟳" in template  # Queued
        assert "⏸" in template  # Awaiting feedback
        assert "⊗" in template  # Cancelled

    def test_contains_error_tooltip(self):
        """Test that template includes error tooltip for failed status."""
        template = render_status_cell_slot()
        assert "<q-tooltip" in template
        assert "props.row.error" in template
        assert "max-width" in template

    def test_contains_color_mappings(self):
        """Test that template includes proper color attributes."""
        template = render_status_cell_slot()
        assert 'color="red"' in template  # Failed
        assert 'color="green"' in template  # Completed
        assert 'color="teal"' in template  # Running
        assert 'color="blue"' in template  # Queued
        assert 'color="orange"' in template  # Awaiting feedback
        assert 'color="grey"' in template  # Cancelled/default

    def test_template_is_vue_compatible(self):
        """Test that template uses valid Vue.js syntax."""
        template = render_status_cell_slot()
        # Vue directives
        assert "v-if" in template
        assert "v-else-if" in template
        assert "v-else" in template


class TestRenderErrorCard:
    """render_error_card renders the given error details and job state into the page."""

    def test_render_error_card_called(self):
        """The card's labels carry the error details and job state, and the raw
        output is rendered as a code block."""
        from nicegui import ui

        from openscientist.job.types import JobInfo
        from openscientist.webapp_components.ui_components import render_error_card

        error_info = {
            "category": "configuration",
            "title": "Test Error",
            "message": "Test message",
            "extracted_error": "Error details",
            "steps": ["Step 1", "Step 2"],
            "raw": "Raw error",
            "contact_admin": True,
        }

        job_info = JobInfo(
            job_id="j1",
            research_question="Q?",
            status=JobStatus.FAILED,
            created_at="2026-02-05T09:00:00",
            failed_at="2026-02-05T10:00:00",
            max_iterations=5,
            iterations_completed=1,
        )

        job_dir = Path("/nonexistent/job/dir")

        # Render for real, then locate the card this call created in the page slot
        before = {id(child) for child in ui.context.slot.children}
        render_error_card(error_info, job_info, job_dir)
        new_children = [c for c in ui.context.slot.children if id(c) not in before]
        assert len(new_children) == 1
        card = new_children[0]

        labels = [e.text for e in card.descendants() if isinstance(e, ui.label)]
        assert "Test Error" in labels
        assert "Test message" in labels
        assert "Step 1" in labels
        assert "Step 2" in labels
        assert any("administrator" in label for label in labels)
        assert "Error Category: Configuration" in labels
        assert "Job Status: failed" in labels
        assert "Iterations Completed: 1/5" in labels
        assert "Failed At: 2026-02-05T10:00:00" in labels

        code_blocks = [e for e in card.descendants() if type(e).__name__ == "Code"]
        assert len(code_blocks) == 1
        assert code_blocks[0]._props["content"] == "Raw error"


class TestPmidLinkParsing:
    """Tests for PMID link parsing in render_text_with_pmid_links."""

    # Regex pattern used in render_text_with_pmid_links
    PMID_PATTERN = re.compile(r"(PMID[:\s]+)(\d{5,8}(?:\s*,\s*\d{5,8})*)", re.IGNORECASE)

    def test_single_pmid_with_colon(self):
        """Test matching single PMID with colon format."""
        text = "As shown in PMID: 12345678, the results..."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(1) == "PMID: "
        assert matches[0].group(2) == "12345678"

    def test_single_pmid_without_colon(self):
        """Test matching single PMID without colon format."""
        text = "Reference PMID 87654321 supports this finding."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(1) == "PMID "
        assert matches[0].group(2) == "87654321"

    def test_comma_separated_pmids(self):
        """Test matching comma-separated PMIDs."""
        text = "(PMID: 12723803, 10638796, 41121397)"
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(1) == "PMID: "
        assert matches[0].group(2) == "12723803, 10638796, 41121397"

    def test_pmid_case_insensitive(self):
        """Test case insensitivity."""
        text = "pmid: 12345678"
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(2) == "12345678"

    def test_pmid_with_year(self):
        """Test PMID followed by year in parentheses."""
        text = "PMID 41514787 (2025): The study shows..."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].group(2) == "41514787"

    def test_multiple_pmid_references(self):
        """Test multiple separate PMID references in text."""
        text = "See PMID: 11111111 and also PMID: 22222222 for details."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 2
        assert matches[0].group(2) == "11111111"
        assert matches[1].group(2) == "22222222"

    def test_no_pmid_in_text(self):
        """Test text with no PMIDs."""
        text = "This is plain text without any references."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 0

    def test_pmid_at_start_of_text(self):
        """Test PMID at the beginning of text."""
        text = "PMID: 12345678 shows evidence of..."
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].start() == 0

    def test_pmid_at_end_of_text(self):
        """Test PMID at the end of text."""
        text = "Evidence from PMID: 12345678"
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        assert matches[0].end() == len(text)

    def test_pmid_boundary_lengths(self):
        """Test PMIDs at boundary lengths (5 to 8 digits)."""
        # Minimum supported length: 5 digits
        matches = list(self.PMID_PATTERN.finditer("PMID: 12345"))
        assert len(matches) == 1
        assert matches[0].group(2) == "12345"

        # Maximum supported length: 8 digits
        matches = list(self.PMID_PATTERN.finditer("PMID: 12345678"))
        assert len(matches) == 1
        assert matches[0].group(2) == "12345678"

        # Too long (9 digits) - should only match first 8
        text = "PMID: 123456789"
        matches = list(self.PMID_PATTERN.finditer(text))
        assert len(matches) == 1
        # The regex will match 12345678 and leave 9 behind
        assert matches[0].group(2) == "12345678"

    def test_short_year_like_number_is_not_treated_as_pmid(self):
        """Test that short 4-digit year-like numbers do not match."""
        matches = list(self.PMID_PATTERN.finditer("PMID: 2025"))
        assert len(matches) == 0

    def test_html_escape_in_text(self):
        """Test that special characters would be escaped properly."""
        # Test the html.escape function behavior
        text = "Evidence <script>alert('xss')</script> from PMID: 12345678"
        escaped = html.escape(text)
        assert "<script>" not in escaped
        assert "&lt;script&gt;" in escaped

    def test_comma_separated_extraction(self):
        """Test splitting comma-separated PMIDs."""
        pmid_list = "12723803, 10638796, 41121397"
        pmids = [p.strip() for p in pmid_list.split(",")]
        assert len(pmids) == 3
        assert pmids[0] == "12723803"
        assert pmids[1] == "10638796"
        assert pmids[2] == "41121397"

    def test_pubmed_url_generation(self):
        """Test that PubMed URLs are generated correctly."""
        pmid = "12345678"
        url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/"
        assert url == "https://pubmed.ncbi.nlm.nih.gov/12345678/"

    def test_transform_pmid_references_leaves_years_plain(self):
        """A 4-digit year after PMID should stay as plain text, not a badge."""
        transformed = transform_pmid_references("Background note PMID: 2025")

        assert "pubmed-badge" not in transformed
        assert transformed == "Background note PMID: 2025"


class TestInlineBadgeMarkup:
    """Inline badges should keep icon and text on one line."""

    def test_pubmed_badge_html_keeps_inline_layout(self):
        badge_html = _get_pubmed_badge_html("12345678")

        assert 'class="pubmed-badge"' in badge_html
        assert "display:inline-flex" in badge_html
        assert "white-space:nowrap" in badge_html
        assert "display:inline !important" in badge_html

    def test_job_id_badge_html_keeps_inline_layout(self):
        badge_html = _get_job_id_badge_html("12345678-1234-1234-1234-1234567890ab")

        assert 'class="job-id-badge"' in badge_html
        assert "display:inline-flex" in badge_html
        assert "white-space:nowrap" in badge_html
        assert "display:inline !important" in badge_html

    def test_job_id_slot_keeps_icon_inline(self):
        template = render_job_id_slot()

        assert "display:inline-flex" in template
        assert "display:inline !important" in template


class TestBadgeStylesRegisteredOnceAtBootstrap:
    """add_head_html(shared=True) appends to a process-global string with no
    dedup, so badge CSS/JS must be registered exactly once, at app bootstrap
    (register_badge_head_html, called from web_app._configure_host_app) --
    never from per-render code, no matter how many badges get rendered."""

    def test_register_badge_head_html_registers_both_style_blocks(self):
        """Both badge style blocks land in the shared head HTML every page receives."""
        from nicegui.client import Client

        before = Client.shared_head_html
        register_badge_head_html()
        added = Client.shared_head_html[len(before) :]

        # The PubMed badge CSS and its click handler script
        assert ".pubmed-badge" in added
        assert "window._pubmedClickHandlerAdded" in added
        # The job ID badge CSS and its click handler script
        assert ".job-id-badge" in added
        assert "window._jobIdClickHandlerAdded" in added
        assert added.count("<style>") == 2

    def test_rendering_many_badges_never_touches_add_head_html(self):
        from nicegui.client import Client

        before = Client.shared_head_html
        for i in range(50):
            render_pmid_badge(str(12000000 + i))
            render_job_id_badge(f"12345678-1234-1234-1234-{i:012d}")
            render_text_with_pmid_links(f"See PMID: {12000000 + i}")

        assert Client.shared_head_html == before

    def test_inject_thinking_status_styles_adds_head_html(self):
        """The thinking-status CSS lands in the shared head HTML."""
        from nicegui.client import Client

        before = Client.shared_head_html
        _inject_thinking_status_styles()
        added = Client.shared_head_html[len(before) :]

        assert ".thinking-label" in added
        assert "openscientist-pulse-text" in added

    def test_rendering_many_thinking_statuses_never_touches_add_head_html(self):
        """render_thinking_status is refreshed on a 2-second poll timer for any
        actively-watched running job -- this is the call site that produced
        the original leak, so it must never call add_head_html itself."""
        from nicegui.client import Client

        before = Client.shared_head_html
        for _ in range(50):
            render_thinking_status("Searching PubMed...")

        assert Client.shared_head_html == before


class TestRenderDeleteDialog:
    """The delete dialog loads the job's current state on show, and its confirm
    handler cancels an active job before deleting it."""

    @staticmethod
    def _mock_ui() -> MagicMock:
        def factory(*_args: object, **_kwargs: object) -> MagicMock:
            element = MagicMock()
            element.__enter__ = MagicMock(return_value=element)
            element.__exit__ = MagicMock(return_value=False)
            element.classes.return_value = element
            return element

        mock = MagicMock()
        for name in ("card", "column", "row", "element", "expansion"):
            setattr(mock, name, factory)
        return mock

    @staticmethod
    def _running_manager(job_id: str) -> SimpleNamespace:
        from openscientist.job.types import JobInfo

        return SimpleNamespace(
            get_job=AsyncMock(
                return_value=JobInfo(
                    job_id=job_id,
                    research_question="Q?",
                    status=JobStatus.RUNNING,
                    created_at="2026-01-01T00:00:00",
                )
            ),
            cancel_job=AsyncMock(),
            delete_job=AsyncMock(),
            ACTIVE_STATUSES=JobManager.ACTIVE_STATUSES,
        )

    async def test_showing_the_dialog_uses_the_job_s_current_state(self):
        from openscientist.webapp_components.ui_components import render_delete_dialog

        job_id = "12345678-1234-1234-1234-123456789012"
        mock_ui = self._mock_ui()
        manager = self._running_manager(job_id)
        with patch("openscientist.webapp_components.ui_components.ui", mock_ui):
            dialog = render_delete_dialog(job_id, manager)
            show_calls = [c for c in dialog.on.call_args_list if c.args and c.args[0] == "show"]
            assert show_calls, "the dialog must reload its content when shown"
            await show_calls[0].args[1]()

        # The dialog warns that the running job is cancelled before deletion.
        labels = [c.args[0] for c in mock_ui.label.call_args_list if c.args]
        assert "Delete Job" in labels
        assert "This job is currently active and will be cancelled before deletion." in labels
        manager.get_job.assert_awaited_once_with(job_id)

    async def test_confirm_cancels_the_active_job_before_deleting_it(self):
        from openscientist.webapp_components import ui_components as uc

        job_id = str(uuid4())
        owner = str(uuid4())
        mock_ui = self._mock_ui()
        manager = self._running_manager(job_id)
        deleted = {"value": False}
        actions: dict[str, object] = {}

        @contextlib.asynccontextmanager
        async def _admin_session():
            session = MagicMock()
            session.get = AsyncMock(return_value=SimpleNamespace(owner_id=UUID(owner)))
            yield session

        with (
            patch.object(uc, "ui", mock_ui),
            patch.object(uc, "get_current_user_id", return_value=owner),
            patch.object(uc, "get_admin_session", _admin_session),
            patch.object(
                uc, "render_dialog_actions", side_effect=lambda **kwargs: actions.update(kwargs)
            ),
        ):
            uc.render_delete_dialog(job_id, manager, on_deleted=lambda: deleted.update(value=True))
            await actions["on_confirm"]()  # type: ignore[arg-type]

        manager.cancel_job.assert_awaited_once_with(job_id)
        manager.delete_job.assert_awaited_once_with(job_id)
        assert deleted["value"] is True
        messages = [c.args[0] for c in mock_ui.notify.call_args_list if c.args]
        assert f"Job {job_id[-8:]} deleted successfully" in messages
