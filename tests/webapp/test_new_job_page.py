"""Tests for new job page helpers."""

import inspect
from types import SimpleNamespace
from typing import Any

import pytest

from openscientist.webapp_components.pages import new_job
from openscientist.webapp_components.pages.new_job import (
    FREEFORM_TAB,
    GUIDED_TAB,
    _build_upload_session_id,
    _collect_template_inputs,
    _submit_job,
    new_job_page,
)


def test_build_upload_session_id_uses_user_and_client_id():
    """Upload session IDs should be scoped by user and websocket client."""
    client = SimpleNamespace(id="client-abc")
    session_id = _build_upload_session_id("user-123", client)
    assert session_id == "user-123:client-abc"


def test_build_upload_session_id_handles_missing_user_with_anonymous_prefix():
    """Anonymous fallback should still include client identity."""
    client = SimpleNamespace(id="client-xyz")
    session_id = _build_upload_session_id(None, client)
    assert session_id == "anonymous:client-xyz"


def test_submit_job_has_use_hypotheses_parameter():
    """_submit_job must accept use_hypotheses so the form toggle is wired in."""
    sig = inspect.signature(_submit_job)
    assert "use_hypotheses" in sig.parameters


def test_submit_job_has_coinvestigate_mode_at_top_level():
    """coinvestigate_mode must be a top-level parameter of _submit_job."""
    sig = inspect.signature(_submit_job)
    assert "coinvestigate_mode" in sig.parameters


def test_submit_job_wires_template_widgets():
    """_submit_job must accept the tab/template widgets driving guided submissions."""
    sig = inspect.signature(_submit_job)
    for param in (
        "workflow_tabs",
        "freeform_question",
        "guided_question",
        "template_select",
        "template_field_widgets",
    ):
        assert param in sig.parameters


def test_new_job_page_accepts_workflow_query_param():
    """The dashboard discovery link relies on /new?workflow=guided."""
    sig = inspect.signature(inspect.unwrap(new_job_page))
    assert "workflow" in sig.parameters
    assert sig.parameters["workflow"].default == FREEFORM_TAB


def test_collect_template_inputs_freeform_returns_none():
    assert _collect_template_inputs(None, {}) is None


def test_collect_template_inputs_drops_empty_values():
    widgets = {
        "gene-set-enrichment": {
            "organism": SimpleNamespace(value="Homo sapiens"),
            "biological_context": SimpleNamespace(value=""),
        }
    }
    collected = _collect_template_inputs("gene-set-enrichment", widgets)
    assert collected == {"organism": "Homo sapiens"}


class _FakeUI:
    """Captures notify/navigate calls so _submit_job runs without a browser."""

    def __init__(self) -> None:
        self.notifications: list[tuple[str, str]] = []
        self.navigated_to: str | None = None
        self.navigate = SimpleNamespace(to=self._navigate_to)

    def notify(self, message: str, type: str = "") -> None:  # noqa: A002 - match NiceGUI API
        self.notifications.append((message, type))

    def _navigate_to(self, target: str) -> None:
        self.navigated_to = target


@pytest.fixture
def submit_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Stub module globals that require a live NiceGUI/auth context."""
    fake_ui = _FakeUI()
    monkeypatch.setattr(new_job, "ui", fake_ui)
    monkeypatch.setattr(new_job, "get_current_user_id", lambda: "user-1")
    monkeypatch.setattr(new_job, "_persist_uploaded_files", lambda _session_id: [])
    monkeypatch.setattr(new_job, "clear_uploaded_files", lambda _session_id: None)

    created: dict[str, Any] = {}

    class FakeJobManager:
        def create_job(self, **kwargs: Any) -> None:
            created.update(kwargs)

    return {"ui": fake_ui, "job_manager": FakeJobManager(), "created": created}


def _base_kwargs(env: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "job_manager": env["job_manager"],
        "user_can_start_jobs": True,
        "session_id": "user-1:client",
        "workflow_tabs": SimpleNamespace(value=FREEFORM_TAB),
        "freeform_question": SimpleNamespace(value="What pathways respond to cold?"),
        "guided_question": SimpleNamespace(value=""),
        "template_select": SimpleNamespace(value="gene-set-enrichment"),
        "template_field_widgets": {},
        "max_iterations": SimpleNamespace(value=10),
        "use_hypotheses": SimpleNamespace(value=False),
        "coinvestigate_mode": SimpleNamespace(value=False),
    }
    kwargs.update(overrides)
    return kwargs


def test_submit_freeform_passes_question_without_description(submit_env: dict[str, Any]):
    _submit_job(**_base_kwargs(submit_env))
    created = submit_env["created"]
    assert created["research_question"] == "What pathways respond to cold?"
    assert created["description"] is None
    assert submit_env["ui"].navigated_to is not None


def test_submit_guided_generates_question_and_guidance(submit_env: dict[str, Any]):
    widgets = {
        "gene-set-enrichment": {
            "gene_set_label": SimpleNamespace(value="cold-up"),
            "foreground_genes": SimpleNamespace(value="TP53\nBRCA1"),
            "organism": SimpleNamespace(value="Homo sapiens"),
            "database": SimpleNamespace(value="go"),
        }
    }
    _submit_job(
        **_base_kwargs(
            submit_env,
            workflow_tabs=SimpleNamespace(value=GUIDED_TAB),
            template_field_widgets=widgets,
        )
    )
    created = submit_env["created"]
    assert "enrichment" in created["research_question"].lower()
    assert created["description"] is not None
    assert "Methodology Guardrails" in created["description"]


def test_submit_guided_missing_required_field_notifies_and_skips(submit_env: dict[str, Any]):
    widgets = {"gene-set-enrichment": {"organism": SimpleNamespace(value="Homo sapiens")}}
    _submit_job(
        **_base_kwargs(
            submit_env,
            workflow_tabs=SimpleNamespace(value=GUIDED_TAB),
            template_field_widgets=widgets,
        )
    )
    assert submit_env["created"] == {}
    assert any(t == "negative" for _msg, t in submit_env["ui"].notifications)
