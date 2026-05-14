"""Tests for new job page helpers."""

import inspect
from types import SimpleNamespace

from openscientist.webapp_components.pages.new_job import (
    FREEFORM_TEMPLATE_ID,
    _build_template_description,
    _build_upload_session_id,
    _collect_template_input_values,
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
    """Template wiring should be explicit in submit helper signature."""
    sig = inspect.signature(_submit_job)
    assert "template_id" in sig.parameters
    assert "template_inputs" in sig.parameters


def test_build_template_description_is_none_for_freeform():
    """Freeform mode should not inject template-specific job description."""
    assert _build_template_description(FREEFORM_TEMPLATE_ID, {}) is None


def test_build_template_description_includes_missing_required_fields():
    """Template metadata should call out required fields when omitted."""
    description = _build_template_description(
        "go_gene_set_enrichment",
        {"target_gene_set": "TP53\nBRCA1"},
    )
    assert description is not None
    assert "Template selected: GO Gene Set Enrichment" in description
    assert "Template inputs:\n- Target Gene Set: TP53\nBRCA1" in description
    assert "Background Gene Set: [MISSING]" in description
    assert "Ask the scientist to provide missing required inputs" in description


def test_collect_template_input_values_trims_and_handles_missing_value_attr():
    """Template input collection should normalize values from form fields safely."""
    values = _collect_template_input_values(
        {
            "genes": SimpleNamespace(value=" TP53 "),
            "count": SimpleNamespace(value=42),
            "missing": SimpleNamespace(),
        }
    )
    assert values == {"genes": "TP53", "count": "42", "missing": ""}
