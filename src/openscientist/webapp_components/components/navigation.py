"""
Navigation UI components for OpenScientist web interface.

Provides the standard authenticated-page header (desktop nav row, brand
link, hamburger toggle) and its companion mobile right-drawer navigation.
"""

from collections.abc import Callable
from typing import Any, cast

from nicegui import app, ui


def _inject_navigation_responsive_css() -> None:
    """Inject responsive CSS toggling mobile drawer button vs desktop nav."""
    ui.add_css(
        """
        @media (max-width: 1023px) {
            .mobile-menu-btn { display: inline-flex !important; }
            .desktop-nav { display: none !important; }
        }
        @media (min-width: 1024px) {
            .mobile-menu-btn { display: none !important; }
            .desktop-nav { display: flex !important; }
        }
    """
    )


def _build_navigation_items(
    active_page: str | None,
    show_new_job: bool,
    can_start_jobs: bool,
    show_admin: bool,
) -> list[tuple[str, str, str, bool]]:
    """Build ordered navigation entries for both desktop and mobile UIs."""
    nav_items: list[tuple[str, str, str, bool]] = []
    if show_new_job and can_start_jobs:
        nav_items.append(("New", "add", "/new", active_page == "new"))
    nav_items.extend(
        [
            ("Skills", "school", "/skills", active_page == "skills"),
            ("API Keys", "vpn_key", "/api-keys", active_page == "api-keys"),
            ("Docs", "description", "/docs", active_page == "docs"),
        ]
    )
    if show_admin:
        nav_items.append(("Admin", "admin_panel_settings", "/admin", active_page == "admin"))
    return nav_items


def _drawer_click_handler(drawer: ui.element, callback: Callable[[], None]) -> Callable[[], None]:
    """Wrap drawer click callbacks to close the drawer first."""

    def handler() -> None:
        cast(Any, drawer).set_value(False)
        callback()

    return handler


def _drawer_route_handler(drawer: ui.element, route: str) -> Callable[[], None]:
    """Create navigation handler for drawer route buttons."""

    def handler() -> None:
        cast(Any, drawer).set_value(False)
        ui.navigate.to(route)

    return handler


def _render_mobile_drawer(
    drawer: ui.element,
    nav_items: list[tuple[str, str, str, bool]],
    extra_buttons: list[tuple[str, str, Callable[[], None], str]] | None,
    active_style: str,
) -> None:
    """Render right-side mobile drawer navigation."""
    drawer.classes("bg-primary")
    with ui.column().classes("w-full gap-2 p-4"):
        ui.label("Navigation").classes("text-white text-h6 font-bold mb-2")
        for label, icon, on_click, _props in extra_buttons or []:
            ui.button(
                label,
                on_click=_drawer_click_handler(drawer, on_click),
                icon=icon,
            ).props("flat color=white align=left").classes("w-full justify-start")

        for label, icon, route, is_active in nav_items:
            style = active_style if is_active else "flat color=white"
            ui.button(
                label,
                on_click=_drawer_route_handler(drawer, route),
                icon=icon,
            ).props(f"{style} align=left").classes("w-full justify-start")

        ui.separator().classes("bg-white/30 my-2")
        ui.button(
            "Logout",
            on_click=lambda: ui.navigate.to("/auth/logout"),
            icon="logout",
        ).props("flat color=white align=left").classes("w-full justify-start")


def _render_navigation_brand() -> None:
    """Render OpenScientist brand/link in header left section."""
    with (
        ui.link(target="/jobs").classes("no-underline"),
        ui.row().classes("items-center gap-2 cursor-pointer"),
    ):
        with ui.element("div").classes(
            "w-10 h-10 rounded-full bg-white flex items-center justify-center"
        ):
            ui.image("/assets/logo.svg").classes("w-8 h-8").style(
                "width:32px;height:32px;min-width:32px;min-height:32px;"
            )
        ui.label("OpenScientist").classes("text-white text-h5 font-bold")


def _render_desktop_navigation(
    nav_items: list[tuple[str, str, str, bool]],
    extra_buttons: list[tuple[str, str, Callable[[], None], str]] | None,
    active_style: str,
    inactive_style: str,
) -> None:
    """Render desktop navigation button row."""
    nav_row = ui.row().classes("gap-1 desktop-nav")
    nav_row.style("display: flex")
    with nav_row:
        for label, icon, on_click, props in extra_buttons or []:
            btn = ui.button(label, on_click=on_click, icon=icon)
            if props:
                btn.props(props)
        for label, icon, route, is_active in nav_items:
            style = active_style if is_active else inactive_style
            ui.button(
                label,
                on_click=lambda r=route: ui.navigate.to(r),
                icon=icon,
            ).props(style)
        ui.button(
            "Logout",
            on_click=lambda: ui.navigate.to("/auth/logout"),
            icon="logout",
        ).props(inactive_style)


def render_navigator(
    active_page: str | None = None,
    show_new_job: bool = True,
    extra_buttons: list[tuple[str, str, Callable[[], None], str]] | None = None,
) -> None:
    """
    Render the standard navigation header for all authenticated pages.

    Provides consistent navigation across the application with links to
    New Job, Billing, Docs, and Admin pages. The OpenScientist logo/title acts
    as a home button linking to the jobs list.
    """
    _inject_navigation_responsive_css()

    show_admin = app.storage.user.get("is_admin", False)
    can_start_jobs = app.storage.user.get("can_start_jobs", False)
    active_style = "unelevated color=white text-color=primary"
    inactive_style = "flat color=white"
    nav_items = _build_navigation_items(active_page, show_new_job, can_start_jobs, show_admin)

    with ui.right_drawer(value=False).props("overlay behavior=mobile bordered") as drawer:
        _render_mobile_drawer(drawer, nav_items, extra_buttons, active_style)

    with ui.header().classes("items-center justify-between"):
        _render_navigation_brand()
        hamburger = ui.button(icon="menu", on_click=lambda: drawer.set_value(True)).props(
            "flat color=white"
        )
        hamburger.style("display: none").classes("mobile-menu-btn")
        _render_desktop_navigation(nav_items, extra_buttons, active_style, inactive_style)
