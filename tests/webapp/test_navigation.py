"""Tests for navigation UI components module."""

from unittest.mock import patch

from openscientist.webapp_components.components.navigation import (
    _build_navigation_items,
    _inject_navigation_responsive_css,
    render_navigator,
)


class TestBuildNavigationItems:
    """Tests for _build_navigation_items function."""

    def test_core_items_only_when_new_and_admin_hidden(self):
        """Test that only Skills, API Keys, and Docs appear with both flags off."""
        items = _build_navigation_items(
            active_page=None, show_new_job=False, can_start_jobs=False, show_admin=False
        )
        assert items == [
            ("Skills", "school", "/skills", False),
            ("API Keys", "vpn_key", "/api-keys", False),
            ("Docs", "description", "/docs", False),
        ]

    def test_new_item_included_when_show_new_job_and_can_start_jobs(self):
        """Test that New appears first when both show_new_job and can_start_jobs are true."""
        items = _build_navigation_items(
            active_page=None, show_new_job=True, can_start_jobs=True, show_admin=False
        )
        assert items[0] == ("New", "add", "/new", False)
        assert len(items) == 4

    def test_new_item_hidden_when_show_new_job_true_but_cannot_start_jobs(self):
        """Test that New is hidden if the caller cannot start jobs, even if requested."""
        items = _build_navigation_items(
            active_page=None, show_new_job=True, can_start_jobs=False, show_admin=False
        )
        assert all(label != "New" for label, _icon, _route, _active in items)

    def test_new_item_hidden_when_can_start_jobs_but_show_new_job_false(self):
        """Test that New is hidden when the page opts out even if jobs can be started."""
        items = _build_navigation_items(
            active_page=None, show_new_job=False, can_start_jobs=True, show_admin=False
        )
        assert all(label != "New" for label, _icon, _route, _active in items)

    def test_admin_item_appended_last_when_show_admin_true(self):
        """Test that Admin appears as the last item when show_admin is true."""
        items = _build_navigation_items(
            active_page=None, show_new_job=False, can_start_jobs=False, show_admin=True
        )
        assert items[-1] == ("Admin", "admin_panel_settings", "/admin", False)

    def test_admin_item_absent_when_show_admin_false(self):
        """Test that Admin is absent when show_admin is false."""
        items = _build_navigation_items(
            active_page=None, show_new_job=False, can_start_jobs=False, show_admin=False
        )
        assert all(label != "Admin" for label, _icon, _route, _active in items)

    def test_full_item_order_with_all_flags_enabled(self):
        """Test the complete ordering of items when every flag is enabled."""
        items = _build_navigation_items(
            active_page=None, show_new_job=True, can_start_jobs=True, show_admin=True
        )
        assert [label for label, _icon, _route, _active in items] == [
            "New",
            "Skills",
            "API Keys",
            "Docs",
            "Admin",
        ]

    def test_active_page_marks_matching_item_active(self):
        """Test that the item matching active_page has its active flag set."""
        items = _build_navigation_items(
            active_page="skills", show_new_job=True, can_start_jobs=True, show_admin=True
        )
        active_labels = [label for label, _icon, _route, is_active in items if is_active]
        assert active_labels == ["Skills"]

    def test_active_page_none_marks_nothing_active(self):
        """Test that no item is active when active_page is None."""
        items = _build_navigation_items(
            active_page=None, show_new_job=True, can_start_jobs=True, show_admin=True
        )
        assert all(is_active is False for _label, _icon, _route, is_active in items)

    def test_active_page_admin_marks_admin_active(self):
        """Test that active_page='admin' marks the Admin item active."""
        items = _build_navigation_items(
            active_page="admin", show_new_job=False, can_start_jobs=False, show_admin=True
        )
        assert items[-1] == ("Admin", "admin_panel_settings", "/admin", True)


class TestInjectNavigationResponsiveCss:
    """Tests for _inject_navigation_responsive_css function."""

    @patch("openscientist.webapp_components.components.navigation.ui")
    def test_add_css_called_once(self, mock_ui):
        """Test that ui.add_css is invoked exactly once."""
        _inject_navigation_responsive_css()
        mock_ui.add_css.assert_called_once()

    @patch("openscientist.webapp_components.components.navigation.ui")
    def test_css_contains_key_selectors(self, mock_ui):
        """Test that the injected CSS contains the mobile/desktop nav selectors."""
        _inject_navigation_responsive_css()
        css = mock_ui.add_css.call_args.args[0]
        assert ".mobile-menu-btn" in css
        assert ".desktop-nav" in css


class TestRenderNavigator:
    """Smoke tests for render_navigator function."""

    @patch("openscientist.webapp_components.components.navigation.app")
    @patch("openscientist.webapp_components.components.navigation.ui")
    def test_render_navigator_runs_without_error(self, mock_ui, mock_app):
        """Test that render_navigator executes end-to-end without raising."""
        mock_app.storage.user.get.return_value = False

        render_navigator(active_page="skills")

        mock_ui.add_css.assert_called_once()
        mock_ui.right_drawer.assert_called_once()
        mock_ui.header.assert_called_once()
        assert mock_ui.button.call_count > 0


class TestRenderNavigatorReexportedFromUiComponents:
    """
    Navigation was extracted to openscientist.webapp_components.components.navigation.
    This test guards the backward-compatibility re-export from ui_components, since
    production page modules across the app still import render_navigator from there.
    """

    def test_render_navigator_is_same_object_in_both_modules(self):
        """ui_components must expose the exact same render_navigator function object."""
        from openscientist.webapp_components import ui_components
        from openscientist.webapp_components.components import navigation

        assert ui_components.render_navigator is navigation.render_navigator
