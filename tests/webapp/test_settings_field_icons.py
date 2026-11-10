"""The Font Awesome icons the admin Settings fields show left of their
inputs, and the vendored stylesheets and webfont the page loads for them."""

from __future__ import annotations

import mimetypes
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nicegui import ui
from nicegui.client import Client
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from openscientist import web_app
from openscientist.database.models import Administrator, Session, User
from openscientist.webapp_components.pages import settings_panel
from tests.admin_row import reset_admin_settings_row, set_admin_settings_row
from tests.db_outage import Forwarder
from tests.test_web_app_factory import _skip_page_module_import
from tests.webapp.conftest import ServedPage

# The icon each settings field and the air-gap switch show (see
# plans/job-cost-badges.md), keyed by the row's column.
_EXPECTED_ICONS: dict[str, str] = {
    "max_job_cost_usd": "fa-solid fa-sack-dollar",
    "max_project_spend_total_usd": "fa-solid fa-vault",
    "max_project_spend_24h_usd": "fa-solid fa-calendar-day",
    "warn_project_spend_24h_usd": "fa-solid fa-triangle-exclamation",
    "agent_cpu": "fa-solid fa-microchip",
    "executor_cpu": "fa-solid fa-microchip",
    "agent_memory_mib": "fa-solid fa-memory",
    "executor_memory_mib": "fa-solid fa-memory",
    "agent_timeout_seconds": "fa-solid fa-hourglass-half",
    "executor_timeout_seconds": "fa-solid fa-hourglass-half",
    "max_file_size_mb": "fa-solid fa-file-arrow-up",
    "max_concurrent_jobs": "fa-solid fa-layer-group",
    "scheduler_window_hours": "fa-solid fa-clock",
    "session_duration_days": "fa-solid fa-user-clock",
    "airgapped": "fa-solid fa-plug-circle-xmark",
}


def _noop(*_args, **_kwargs) -> None:
    pass


def _host_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FastAPI:
    """The real host app, with the registrations the static route does not
    depend on switched off the way the factory tests do."""
    monkeypatch.setattr(web_app, "_state", web_app._AppState())
    for name in (
        "_register_health_endpoint",
        "_register_robots_txt",
        "_register_apple_touch_icon_redirect",
        "_register_api_routes",
        "_register_oauth_routes",
        "_register_share_routes",
        "_register_download_routes",
        "_register_review_routes",
        "_initialize_job_manager_runtime",
        "_register_nicegui_static_files",
        "_register_pwa_metadata",
        "register_badge_head_html",
        "_inject_density_styles",
        "_inject_thinking_status_styles",
    ):
        monkeypatch.setattr(web_app, name, _noop)
    monkeypatch.setattr(web_app.ui, "run_with", _noop)
    _skip_page_module_import(monkeypatch)
    return web_app.create_app(tmp_path / "jobs")


@pytest.fixture
def without_system_mime_types(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The web image has no /etc/mime.types, so its types come from Python's
    built-in map alone. init() appends its files argument to knownfiles, so
    the list itself is emptied."""
    monkeypatch.setattr(mimetypes, "knownfiles", [])
    mimetypes.init()
    yield
    monkeypatch.undo()
    mimetypes.init()


@pytest.mark.usefixtures("without_system_mime_types")
@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/fa/css/fontawesome.min.css", "text/css"),
        ("/fa/css/solid.min.css", "text/css"),
        ("/fa/webfonts/fa-solid-900.woff2", "font/woff2"),
    ],
)
def test_the_vendored_fontawesome_assets_serve_same_origin_with_their_types(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    path: str,
    content_type: str,
) -> None:
    """The stylesheets and the webfont answer 200 with their real types, so the
    app CSP's self-only style-src and font-src and the nosniff header let the
    browser use them."""
    host_app = _host_app(monkeypatch, tmp_path)
    client = TestClient(host_app)
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].split(";")[0] == content_type
    assert response.headers["x-content-type-options"] == "nosniff"
    csp = response.headers["content-security-policy"]
    assert "style-src 'self'" in csp
    assert "font-src 'self' data:" in csp

    if path.endswith(".css"):
        assert "Font Awesome Free 7.3.1" in response.text
    else:
        expected = (web_app.FA_ASSETS_DIR / "webfonts" / "fa-solid-900.woff2").read_bytes()
        assert response.content == expected


@pytest_asyncio.fixture(autouse=True)
async def _default_row_after(test_engine: AsyncEngine) -> AsyncIterator[None]:
    """Every test leaves the default row the suite expects."""
    yield
    await reset_admin_settings_row(test_engine)


@pytest_asyncio.fixture
async def settings_admin(test_engine: AsyncEngine) -> AsyncIterator[tuple[User, str]]:
    """A committed admin with a live session, the row the panel reads from."""
    async with AsyncSession(test_engine, expire_on_commit=False) as db:
        admin = User(
            email=f"settings-admin-{uuid4().hex}@admin.test", name="Admin", is_approved=True
        )
        db.add(admin)
        await db.flush()
        db.add(Administrator(user_id=admin.id))
        login = Session(user_id=admin.id, expires_at=datetime.now(UTC) + timedelta(days=1))
        db.add(login)
        await db.commit()
    yield admin, str(login.id)
    async with AsyncSession(test_engine) as db:
        await db.execute(delete(User).where(User.id == admin.id))
        await db.commit()


def _icons_left_of(switch: ui.switch) -> list[str]:
    """The icon names rendered before the switch in its row, so an icon moved
    to the right of the toggle or into its label does not count."""
    assert switch.parent_slot is not None
    siblings = switch.parent_slot.children
    before = siblings[: siblings.index(switch)]
    return [child.name for child in before if isinstance(child, ui.icon)]


def _field_icons(client: Client) -> dict[str, str]:
    """The icon name of each settings field, keyed by column. Number fields
    show it in the slot before the input's border, the switch left of the
    toggle."""
    labels: dict[str, str] = {
        (f"{label} ({unit})" if unit else label): column
        for column, label, unit, *_ in settings_panel._FIELDS
    }
    labels["Air-gap"] = settings_panel._AIRGAP[0]
    found: dict[str, str] = {}
    for element in client.elements.values():
        if isinstance(element, ui.switch):
            column = labels.get(element.text)
            icons = _icons_left_of(element)
            if column is not None and icons:
                found[column] = icons[0]
        elif isinstance(element, ui.number) and element.label is not None:
            column = labels.get(element.label)
            slot = element.slots.get("before")
            if column is None or slot is None:
                continue
            for child in slot.children:
                if isinstance(child, ui.icon):
                    found[column] = child.name
    return found


@pytest.mark.asyncio
async def test_every_settings_field_shows_its_icon_left_of_the_input(
    database: Forwarder,
    test_engine: AsyncEngine,
    settings_admin: tuple[User, str],
    served_page: ServedPage,
) -> None:
    """Each field shows the design's icon outside the input's border, the
    air-gap switch left of its toggle, and no field shows an icon of its own
    or none at all."""
    _, _token = settings_admin
    await set_admin_settings_row(test_engine)
    _, sent = await served_page(_token, settings_panel.render_settings_panel, _field_icons)
    assert sent == _EXPECTED_ICONS


def test_every_settings_field_and_the_air_gap_switch_map_to_an_icon() -> None:
    """The icon map covers _FIELDS and the air-gap switch exactly, so a new
    field without an icon fails the test instead of rendering blank."""
    columns = {column for column, *_ in settings_panel._FIELDS} | {settings_panel._AIRGAP[0]}
    assert columns == set(settings_panel._FIELD_ICONS)
