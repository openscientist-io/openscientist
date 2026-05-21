"""Tests for the guided job template registry and resolution logic."""

import pytest

from openscientist.job_templates import (
    FREEFORM_DEFAULT_MAX_ITERATIONS,
    FREEFORM_TEMPLATE_ID,
    TemplateValidationError,
    build_template_research_question,
    default_max_iterations_for_template,
    get_job_template,
    list_job_templates,
    normalize_template_id,
    resolve_template_submission,
    workflow_options,
)

EXPECTED_TEMPLATE_IDS = {
    "gene-set-enrichment",
    "evidence-synthesis",
    "computational-simulation",
    "microbiome-differential-abundance",
    "variant-interpretation",
}


class TestRegistry:
    def test_builtin_templates_present(self):
        ids = {template.id for template in list_job_templates()}
        assert ids == EXPECTED_TEMPLATE_IDS

    def test_workflow_options_include_freeform_first(self):
        options = workflow_options()
        assert FREEFORM_TEMPLATE_ID in options
        assert next(iter(options)) == FREEFORM_TEMPLATE_ID
        for template_id in EXPECTED_TEMPLATE_IDS:
            assert template_id in options

    @pytest.mark.parametrize("value", ["", "  ", FREEFORM_TEMPLATE_ID, None])
    def test_normalize_freeform_to_none(self, value):
        assert normalize_template_id(value) is None

    def test_normalize_strips_whitespace(self):
        assert normalize_template_id("  gene-set-enrichment  ") == "gene-set-enrichment"

    def test_get_unknown_template_returns_none(self):
        assert get_job_template("does-not-exist") is None

    def test_default_iterations(self):
        assert default_max_iterations_for_template(None) == FREEFORM_DEFAULT_MAX_ITERATIONS
        assert default_max_iterations_for_template("gene-set-enrichment") == 2
        assert default_max_iterations_for_template("evidence-synthesis") == 4


class TestInputValidation:
    def test_missing_required_field_raises(self):
        template = get_job_template("gene-set-enrichment")
        assert template is not None
        with pytest.raises(TemplateValidationError, match="required"):
            template.validate_inputs({"organism": "Homo sapiens"})

    def test_invalid_select_value_raises(self):
        template = get_job_template("gene-set-enrichment")
        assert template is not None
        with pytest.raises(TemplateValidationError, match="must be one of"):
            template.validate_inputs(
                {
                    "gene_set_label": "cold-up",
                    "foreground_genes": "TP53\nBRCA1",
                    "organism": "Homo sapiens",
                    "database": "not-a-db",
                }
            )

    def test_valid_inputs_normalized(self):
        template = get_job_template("gene-set-enrichment")
        assert template is not None
        normalized = template.validate_inputs(
            {
                "gene_set_label": "  cold-up  ",
                "foreground_genes": "TP53\nBRCA1",
                "organism": "Homo sapiens",
                "database": "go",
            }
        )
        assert normalized["gene_set_label"] == "cold-up"
        assert normalized["database"] == "go"
        # Empty optional fields are dropped.
        assert "biological_context" not in normalized


class TestResearchQuestionAndGuidance:
    def _valid_gene_set_inputs(self) -> dict[str, str]:
        return {
            "gene_set_label": "upregulated genes in cold-treated samples",
            "foreground_genes": "TP53\nBRCA1\nEGFR",
            "background_genes": "all assayed genes",
            "organism": "Homo sapiens",
            "database": "go",
            "biological_context": "cold exposure",
        }

    def test_research_question_weaves_inputs(self):
        question = build_template_research_question(
            "gene-set-enrichment", self._valid_gene_set_inputs()
        )
        assert "Homo sapiens" in question
        assert "GO enrichment" in question
        assert "background gene set" in question
        assert "cold exposure" in question

    def test_research_question_flags_missing_background(self):
        inputs = self._valid_gene_set_inputs()
        del inputs["background_genes"]
        question = build_template_research_question("gene-set-enrichment", inputs)
        assert "missing background" in question

    def test_guidance_contains_guardrails_and_inputs(self):
        template = get_job_template("gene-set-enrichment")
        assert template is not None
        normalized = template.validate_inputs(self._valid_gene_set_inputs())
        guidance = template.build_guidance(normalized)
        assert "Methodology Guardrails" in guidance
        assert "Report Expectations" in guidance
        assert "Visualization Expectations" in guidance
        assert "Structured Inputs" in guidance
        assert "multiple-test correction" in guidance

    def test_preview_freeform_returns_empty(self):
        assert build_template_research_question(None, None) == ""


class TestResolution:
    def test_freeform_passthrough(self):
        resolution = resolve_template_submission(
            template_id=FREEFORM_TEMPLATE_ID,
            template_inputs=None,
            research_question="What pathways are affected by hypothermia?",
        )
        assert resolution.research_question == "What pathways are affected by hypothermia?"
        assert resolution.description is None
        assert resolution.default_max_iterations == FREEFORM_DEFAULT_MAX_ITERATIONS

    def test_freeform_empty_question_raises(self):
        with pytest.raises(TemplateValidationError, match="research question"):
            resolve_template_submission(
                template_id=None, template_inputs=None, research_question="  "
            )

    def test_template_generates_question_and_guidance(self):
        resolution = resolve_template_submission(
            template_id="gene-set-enrichment",
            template_inputs={
                "gene_set_label": "cold-up",
                "foreground_genes": "TP53\nBRCA1",
                "organism": "Homo sapiens",
                "database": "go",
            },
            research_question=None,
        )
        assert "gene set enrichment" in resolution.research_question.lower()
        assert resolution.description is not None
        assert "Methodology Guardrails" in resolution.description
        assert resolution.default_max_iterations == 2

    def test_template_missing_required_raises(self):
        with pytest.raises(TemplateValidationError, match="required"):
            resolve_template_submission(
                template_id="gene-set-enrichment",
                template_inputs={"organism": "Homo sapiens"},
                research_question=None,
            )

    def test_user_question_overrides_generated(self):
        resolution = resolve_template_submission(
            template_id="gene-set-enrichment",
            template_inputs={
                "gene_set_label": "cold-up",
                "foreground_genes": "TP53",
                "organism": "Homo sapiens",
                "database": "go",
            },
            research_question="My own custom question about enrichment in this dataset.",
        )
        assert resolution.research_question == (
            "My own custom question about enrichment in this dataset."
        )
        # Guidance is still attached from the template.
        assert resolution.description is not None
