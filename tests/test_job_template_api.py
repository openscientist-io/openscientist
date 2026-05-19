"""API payload tests for guided job templates."""

import pytest
from fastapi.exceptions import RequestValidationError

from openscientist.api.endpoints.jobs import _validate_job_payload


def test_job_create_accepts_template_without_research_question() -> None:
    """API clients can submit structured template inputs and let OS generate the question."""
    payload = _validate_job_payload(
        {
            "template_id": "variant-interpretation",
            "template_inputs": {
                "variant_input": "uploaded variants.vcf",
                "organism_build": "human-grch38",
                "phenotype_context": "cardiomyopathy",
                "interpretation_mode": "exploratory",
            },
        }
    )

    assert payload.research_question is None
    assert payload.template_id == "variant-interpretation"
    assert payload.template_inputs is not None
    assert payload.template_inputs["organism_build"] == "human-grch38"


def test_job_create_parses_template_inputs_json_string() -> None:
    """Multipart form handling can pass template_inputs as a JSON string."""
    payload = _validate_job_payload(
        {
            "template_id": "computational-simulation",
            "template_inputs": (
                '{"system_model": "MAPK pathway", "model_type": "ode", '
                '"parameters": "k1=0.1", "perturbations": "MEK inhibition", '
                '"observables": "ERK activity"}'
            ),
        }
    )

    assert payload.template_inputs is not None
    assert payload.template_inputs["system_model"] == "MAPK pathway"


def test_job_create_rejects_freeform_without_research_question() -> None:
    """Freeform API submissions still need an explicit research question."""
    with pytest.raises(RequestValidationError):
        _validate_job_payload({"max_iterations": 5})
