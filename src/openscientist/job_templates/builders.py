"""Question-builder hooks for guided job templates.

Most template data lives in YAML. Keep Python here only for wording that needs
conditional logic beyond simple string interpolation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

QuestionBuilder = Callable[[Mapping[str, Any]], str]


def _text(inputs: Mapping[str, Any], key: str, fallback: str = "") -> str:
    value = inputs.get(key, fallback)
    return str(value).strip() if value is not None else fallback


def build_gene_set_question(inputs: Mapping[str, Any]) -> str:
    database = _text(inputs, "database", "auto")
    organism = _text(inputs, "organism", "the specified organism")
    label = _text(inputs, "gene_set_label", "the provided gene set")
    background = _text(inputs, "background_genes")
    context = _text(inputs, "biological_context")
    background_clause = (
        "using the provided background gene set"
        if background
        else "and explicitly assess the limitations of any missing background set"
    )
    context_clause = f" in the context of {context}" if context else ""
    database_clause = "selecting the most appropriate enrichment database"
    if database != "auto":
        database_clause = f"using {database.upper()} enrichment"
    return (
        f"Perform gene set enrichment analysis for {label} in {organism}, "
        f"{background_clause}, {database_clause}{context_clause}. "
        "Report statistically significant terms after multiple-test correction and "
        "interpret the biological themes only after deterministic enrichment statistics."
    )


def build_evidence_question(inputs: Mapping[str, Any]) -> str:
    focus = _text(inputs, "research_focus")
    population = _text(inputs, "population")
    outcome = _text(inputs, "outcome")
    exposure = _text(inputs, "exposure")
    mode = _text(inputs, "synthesis_mode", "evidence-synthesis")
    exposure_clause = f" related to {exposure}" if exposure else ""
    return (
        f"Conduct an evidence synthesis for {focus} in {population}{exposure_clause}, "
        f"with emphasis on {outcome}. Use a {mode.replace('-', ' ')} approach, "
        "extract evidence systematically, assess study quality and heterogeneity, and "
        "separate evidence strength from interpretation."
    )


def build_simulation_question(inputs: Mapping[str, Any]) -> str:
    system_model = _text(inputs, "system_model")
    model_type = _text(inputs, "model_type")
    perturbations = _text(inputs, "perturbations")
    observables = _text(inputs, "observables")
    return (
        f"Build or evaluate a {model_type.replace('-', ' ')} computational simulation for "
        f"{system_model}. Test these perturbations: {perturbations}. Track these "
        f"observables: {observables}. State assumptions explicitly, run sensitivity checks, "
        "and distinguish simulated behavior from empirical evidence."
    )


def build_microbiome_question(inputs: Mapping[str, Any]) -> str:
    comparison = _text(inputs, "comparison")
    grouping = _text(inputs, "grouping_variable")
    metadata = _text(inputs, "metadata_description")
    covariates = _text(inputs, "covariates")
    taxonomic_level = _text(inputs, "taxonomic_level", "auto")
    covariate_clause = f" adjusting for {covariates}" if covariates else ""
    tax_clause = (
        "selecting an appropriate taxonomic level"
        if taxonomic_level == "auto"
        else f"at the {taxonomic_level} taxonomic level"
    )
    return (
        f"Analyze microbiome differential abundance for {comparison} using "
        f"{grouping} as the primary grouping variable and metadata context: {metadata}. "
        f"Evaluate compositionality, sequencing depth, confounding{covariate_clause}, "
        f"and multiple testing while reporting results {tax_clause}."
    )


def build_variant_question(inputs: Mapping[str, Any]) -> str:
    variant_input = _text(inputs, "variant_input")
    organism_build = _text(inputs, "organism_build")
    phenotype = _text(inputs, "phenotype_context")
    mode = _text(inputs, "interpretation_mode")
    inheritance = _text(inputs, "inheritance_model", "unknown")
    genes = _text(inputs, "affected_genes")
    gene_clause = f" Prioritize known affected genes or loci: {genes}." if genes else ""
    return (
        f"Interpret the provided variants ({variant_input}) for {organism_build} in the "
        f"context of {phenotype}. Use {mode.replace('-', ' ')} mode with inheritance model "
        f"{inheritance.replace('-', ' ')}.{gene_clause} Annotate variants with deterministic "
        "sources first, rank evidence explicitly, and separate pathogenicity evidence, "
        "functional plausibility, phenotype match, and uncertainty."
    )


QUESTION_BUILDERS: dict[str, QuestionBuilder] = {
    "gene-set-enrichment": build_gene_set_question,
    "evidence-synthesis": build_evidence_question,
    "computational-simulation": build_simulation_question,
    "microbiome-differential-abundance": build_microbiome_question,
    "variant-interpretation": build_variant_question,
}


def get_question_builder(builder_id: str) -> QuestionBuilder:
    """Return a registered Python question-builder hook."""
    return QUESTION_BUILDERS[builder_id]
