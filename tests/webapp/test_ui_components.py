"""Tests for UI components module."""

from unittest.mock import patch

from openscientist.webapp_components.ui_components import (
    OPENSCIENTIST_GITHUB_URL,
    OPENSCIENTIST_PAPER_URL,
    OPENSCIENTIST_RELEASE_URL,
    STATUS_COLORS,
    STATUS_ICONS,
    _inject_thinking_status_styles,
    get_project_resource_links,
    get_status_badge_props,
    register_badge_head_html,
    render_job_id_badge,
    render_job_id_slot,
    render_pmid_badge,
    render_status_cell_slot,
    render_text_with_pmid_links,
    render_thinking_status,
    transform_pmid_references,
)


class TestActionsSymbolsReexportedFromUiComponents:
    """
    render_job_action_buttons was extracted to
    openscientist.webapp_components.components.actions. This test guards the
    backward-compatibility re-export from ui_components, since production code
    across the app still imports it directly from this module.
    """

    def test_render_job_action_buttons_is_same_object_in_both_modules(self):
        """ui_components must expose the exact same render_job_action_buttons object."""
        from openscientist.webapp_components import ui_components
        from openscientist.webapp_components.components import actions

        assert ui_components.render_job_action_buttons is actions.render_job_action_buttons

    def test_named_import_from_ui_components_still_works(self):
        """Exercise the historical `from ui_components import render_job_action_buttons` pattern."""
        from openscientist.webapp_components.ui_components import render_job_action_buttons

        assert callable(render_job_action_buttons)


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


class TestBadgeSymbolsReexportedFromUiComponents:
    """
    Badge components were extracted to
    openscientist.webapp_components.components.badges. These tests guard the
    backward-compatibility re-export from ui_components, since production code
    across the app still imports badge symbols directly from this module.
    """

    REEXPORTED_BADGE_SYMBOLS = [
        "CATEGORY_COLORS",
        "STATUS_COLORS",
        "STATUS_ICONS",
        "get_category_color",
        "get_status_badge_props",
        "render_container_status_badge",
        "render_job_id_badge",
        "render_job_id_slot",
        "render_permission_badge_slot",
        "render_pmid_badge",
        "render_stat_badges",
        "render_status_cell_slot",
        "render_text_with_pmid_links",
        "transform_pmid_references",
    ]

    def test_badge_symbols_are_identical_objects_in_both_modules(self):
        """ui_components must expose the exact same objects as the badges module."""
        from openscientist.webapp_components import ui_components
        from openscientist.webapp_components.components import badges

        for name in self.REEXPORTED_BADGE_SYMBOLS:
            assert hasattr(ui_components, name), f"{name} is not importable from ui_components"
            assert getattr(ui_components, name) is getattr(badges, name), (
                f"ui_components.{name} is not the same object as badges.{name}"
            )

    def test_named_imports_from_ui_components_still_work(self):
        """Exercise the historical `from ui_components import X` usage pattern."""
        from openscientist.webapp_components.ui_components import (
            CATEGORY_COLORS,
            get_category_color,
            render_container_status_badge,
            render_job_id_badge,
            render_job_id_slot,
            render_permission_badge_slot,
            render_pmid_badge,
            render_stat_badges,
            render_text_with_pmid_links,
        )

        assert isinstance(CATEGORY_COLORS, dict)
        assert isinstance(STATUS_COLORS, dict)
        assert isinstance(STATUS_ICONS, dict)
        assert callable(get_category_color)
        assert callable(get_status_badge_props)
        assert callable(render_container_status_badge)
        assert callable(render_job_id_badge)
        assert callable(render_job_id_slot)
        assert callable(render_permission_badge_slot)
        assert callable(render_pmid_badge)
        assert callable(render_stat_badges)
        assert callable(render_status_cell_slot)
        assert callable(render_text_with_pmid_links)
        assert callable(transform_pmid_references)


class TestFormsSymbolsReexportedFromUiComponents:
    """
    render_dialog_actions was extracted to
    openscientist.webapp_components.components.forms. This test guards the
    backward-compatibility re-export from ui_components, since production code
    across the app still imports it directly from this module.
    """

    def test_render_dialog_actions_is_same_object_in_both_modules(self):
        """ui_components must expose the exact same render_dialog_actions object."""
        from openscientist.webapp_components import ui_components
        from openscientist.webapp_components.components import forms

        assert ui_components.render_dialog_actions is forms.render_dialog_actions

    def test_named_import_from_ui_components_still_works(self):
        """Exercise the historical `from ui_components import render_dialog_actions` pattern."""
        from openscientist.webapp_components.ui_components import render_dialog_actions

        assert callable(render_dialog_actions)


class TestTablesSymbolsReexportedFromUiComponents:
    """
    Table slot-template generators were extracted to
    openscientist.webapp_components.components.tables. These tests guard the
    backward-compatibility re-export from ui_components, since production code
    across the app still imports these symbols directly from this module.
    """

    REEXPORTED_TABLE_SYMBOLS = [
        "make_action_button_slot",
        "render_actions_slot_with_delete",
        "render_skill_name_slot",
    ]

    def test_table_symbols_are_identical_objects_in_both_modules(self):
        """ui_components must expose the exact same objects as the tables module."""
        from openscientist.webapp_components import ui_components
        from openscientist.webapp_components.components import tables

        for name in self.REEXPORTED_TABLE_SYMBOLS:
            assert hasattr(ui_components, name), f"{name} is not importable from ui_components"
            assert getattr(ui_components, name) is getattr(tables, name), (
                f"ui_components.{name} is not the same object as tables.{name}"
            )

    def test_named_imports_from_ui_components_still_work(self):
        """Exercise the historical `from ui_components import X` usage pattern."""
        from openscientist.webapp_components.ui_components import (
            make_action_button_slot,
            render_actions_slot_with_delete,
            render_skill_name_slot,
        )

        assert callable(make_action_button_slot)
        assert callable(render_actions_slot_with_delete)
        assert callable(render_skill_name_slot)


class TestAlertsSymbolsReexportedFromUiComponents:
    """
    Alert/banner components were extracted to
    openscientist.webapp_components.components.alerts. These tests guard the
    backward-compatibility re-export from ui_components, since production code
    across the app (and webapp_components/__init__.py) still imports these
    symbols directly from this module.
    """

    REEXPORTED_ALERT_SYMBOLS = [
        "render_alert_banner",
        "render_config_error_banner",
        "render_error_card",
    ]

    def test_alert_symbols_are_identical_objects_in_both_modules(self):
        """ui_components must expose the exact same objects as the alerts module."""
        from openscientist.webapp_components import ui_components
        from openscientist.webapp_components.components import alerts

        for name in self.REEXPORTED_ALERT_SYMBOLS:
            assert hasattr(ui_components, name), f"{name} is not importable from ui_components"
            assert getattr(ui_components, name) is getattr(alerts, name), (
                f"ui_components.{name} is not the same object as alerts.{name}"
            )

    def test_named_imports_from_ui_components_still_work(self):
        """Exercise the historical `from ui_components import X` usage pattern."""
        from openscientist.webapp_components.ui_components import (
            render_alert_banner,
            render_config_error_banner,
            render_error_card,
        )

        assert callable(render_alert_banner)
        assert callable(render_config_error_banner)
        assert callable(render_error_card)


class TestTextSymbolsReexportedFromUiComponents:
    """
    Text formatting/rendering helpers were extracted to
    openscientist.webapp_components.components.text. These tests guard the
    backward-compatibility re-export from ui_components, since production code
    across the app still imports these symbols directly from this module.
    """

    REEXPORTED_TEXT_SYMBOLS = [
        "format_relative_time",
        "format_uptime",
        "render_justified_text",
    ]

    def test_text_symbols_are_identical_objects_in_both_modules(self):
        """ui_components must expose the exact same objects as the text module."""
        from openscientist.webapp_components import ui_components
        from openscientist.webapp_components.components import text

        for name in self.REEXPORTED_TEXT_SYMBOLS:
            assert hasattr(ui_components, name), f"{name} is not importable from ui_components"
            assert getattr(ui_components, name) is getattr(text, name), (
                f"ui_components.{name} is not the same object as text.{name}"
            )

    def test_named_imports_from_ui_components_still_work(self):
        """Exercise the historical `from ui_components import X` usage pattern."""
        from openscientist.webapp_components.ui_components import (
            format_relative_time,
            format_uptime,
            render_justified_text,
        )

        assert callable(format_relative_time)
        assert callable(format_uptime)
        assert callable(render_justified_text)

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
        with patch("openscientist.webapp_components.ui_components.ui.add_head_html") as mock_add:
            register_badge_head_html()

        assert mock_add.call_count == 2

    def test_rendering_many_badges_never_touches_add_head_html(self):
        with (
            patch("openscientist.webapp_components.ui_components.ui.add_head_html") as mock_add,
            patch("openscientist.webapp_components.ui_components.ui.html"),
        ):
            for i in range(50):
                render_pmid_badge(str(12000000 + i))
                render_job_id_badge(f"12345678-1234-1234-1234-{i:012d}")
                render_text_with_pmid_links(f"See PMID: {12000000 + i}")

        mock_add.assert_not_called()

    def test_inject_thinking_status_styles_adds_head_html(self):
        with patch("openscientist.webapp_components.ui_components.ui.add_head_html") as mock_add:
            _inject_thinking_status_styles()

        mock_add.assert_called_once()

    def test_rendering_many_thinking_statuses_never_touches_add_head_html(self):
        """render_thinking_status is refreshed on a 2-second poll timer for any
        actively-watched running job -- this is the call site that produced
        the original leak, so it must never call add_head_html itself."""
        # render_thinking_status builds real elements (`with ui.row()`, `ui.html`),
        # so patch the module's ui wholesale rather than only add_head_html.
        # Creating 50 real rows depends on an ambient NiceGUI slot that other
        # tests can invalidate ("parent element ... has been deleted"), which
        # made this order-dependent. The assertion below is unchanged.
        with patch("openscientist.webapp_components.ui_components.ui") as mock_ui:
            for _ in range(50):
                render_thinking_status("Searching PubMed...")

        mock_ui.add_head_html.assert_not_called()
