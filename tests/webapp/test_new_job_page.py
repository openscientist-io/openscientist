"""Tests for new job page helpers."""

import inspect
from types import SimpleNamespace

from openscientist.webapp_components.pages.new_job import (
    _build_upload_session_id,
    _collect_template_inputs,
    _submit_job,
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


def test_submit_job_has_template_parameters():
    """_submit_job must receive guided workflow widgets from the form."""
    sig = inspect.signature(_submit_job)
    assert "workflow_template" in sig.parameters
    assert "template_field_widgets" in sig.parameters


def test_collect_template_inputs_returns_selected_widget_values():
    """Only the active template's structured inputs should be submitted."""
    widgets = {
        "variant-interpretation": {
            "variant_input": SimpleNamespace(value="uploaded variants.vcf"),
            "phenotype_context": SimpleNamespace(value="cardiomyopathy"),
            "optional_empty": SimpleNamespace(value=""),
        },
        "gene-set-enrichment": {
            "organism": SimpleNamespace(value="Homo sapiens"),
        },
    }

    assert _collect_template_inputs("variant-interpretation", widgets) == {
        "variant_input": "uploaded variants.vcf",
        "phenotype_context": "cardiomyopathy",
    }


def test_collect_template_inputs_ignores_freeform():
    """Freeform jobs should not persist empty template input metadata."""
    widgets = {
        "variant-interpretation": {
            "variant_input": SimpleNamespace(value="uploaded variants.vcf"),
        }
    }

    assert _collect_template_inputs("freeform", widgets) is None
