"""Built-in guided job templates, defined as data.

Templates are part data (fields, guardrails) and part logic (the question
builder weaves key inputs into prose), so they live in Python rather than YAML.
Adding a template is a matter of appending a ``JobTemplate`` to ``BUILTIN_TEMPLATES``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from openscientist.job_templates.types import (
    JobTemplate,
    TemplateField,
    TemplateFieldOption,
)


def _text(inputs: Mapping[str, Any], key: str, fallback: str = "") -> str:
    value = inputs.get(key, fallback)
    return str(value).strip() if value is not None else fallback


def _gene_set_question(inputs: Mapping[str, Any]) -> str:
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


def _variant_question(inputs: Mapping[str, Any]) -> str:
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


def _option(value: str, label: str) -> TemplateFieldOption:
    return TemplateFieldOption(value=value, label=label)


BUILTIN_TEMPLATES: tuple[JobTemplate, ...] = (
    JobTemplate(
        id="gene-set-enrichment",
        version="1",
        name="Gene Set Enrichment",
        summary="Guide enrichment analysis with foreground/background sets and database choice.",
        default_max_iterations=2,
        question_builder=_gene_set_question,
        fields=(
            TemplateField(
                key="gene_set_label",
                label="Gene set label",
                required=True,
                placeholder="e.g., upregulated genes in cold-treated samples",
            ),
            TemplateField(
                key="foreground_genes",
                label="Foreground genes",
                kind="textarea",
                required=True,
                placeholder="Paste gene symbols/IDs, one per line, or describe the uploaded file.",
                rows=5,
            ),
            TemplateField(
                key="background_genes",
                label="Background genes",
                kind="textarea",
                placeholder="Paste the tested/background universe or describe the uploaded file.",
                help_text="Strongly recommended for statistically defensible enrichment.",
                rows=4,
            ),
            TemplateField(
                key="organism",
                label="Organism",
                required=True,
                placeholder="e.g., Homo sapiens",
            ),
            TemplateField(
                key="database",
                label="Enrichment database",
                kind="select",
                required=True,
                default="auto",
                options=(
                    _option("auto", "Auto-select"),
                    _option("go", "Gene Ontology"),
                    _option("kegg", "KEGG"),
                    _option("reactome", "Reactome"),
                ),
            ),
            TemplateField(
                key="biological_context",
                label="Biological context",
                kind="textarea",
                placeholder="Optional condition, tissue, treatment, or phenotype context.",
            ),
        ),
        methodology=(
            "Run or design deterministic enrichment/statistical analysis before interpretation.",
            "Use the provided background set when available; if absent, state the bias this creates.",
            "Apply multiple-test correction and report the correction method.",
            "Do not infer enrichment from latent biological knowledge alone.",
        ),
        report_guidance=(
            "Include enriched terms, adjusted p-values/FDR, effect sizes or overlap counts, "
            "and driving genes.",
            "Separate statistical results from biological interpretation.",
            "Call out database choice and background-universe assumptions.",
        ),
        visualization_guidance=(
            "Prefer bar, dot, or enrichment-map style summaries of top significant terms.",
            "Show foreground/background overlap or leading genes when useful.",
        ),
    ),
    JobTemplate(
        id="variant-interpretation",
        version="1",
        name="Variant Interpretation",
        summary=(
            "Rank variants with phenotype context, inheritance assumptions, "
            "and deterministic annotation first."
        ),
        default_max_iterations=3,
        question_builder=_variant_question,
        fields=(
            TemplateField(
                key="variant_input",
                label="Variants or uploaded VCF",
                kind="textarea",
                required=True,
                placeholder="Paste variants or describe the uploaded VCF/list and relevant columns.",
                rows=4,
            ),
            TemplateField(
                key="organism_build",
                label="Organism and genome build",
                kind="select",
                required=True,
                default="human-grch38",
                options=(
                    _option("human-grch38", "Human GRCh38"),
                    _option("human-grch37", "Human GRCh37"),
                    _option("mouse-grcm39", "Mouse GRCm39"),
                    _option("other", "Other / specify in context"),
                ),
            ),
            TemplateField(
                key="phenotype_context",
                label="Phenotype or disease context",
                kind="textarea",
                required=True,
                placeholder=(
                    "Clinical phenotype, disease, traits, HPO terms, model organism phenotype, "
                    "or cohort context."
                ),
                rows=4,
            ),
            TemplateField(
                key="interpretation_mode",
                label="Interpretation mode",
                kind="select",
                required=True,
                default="exploratory",
                options=(
                    _option("diagnostic", "Diagnostic"),
                    _option("exploratory", "Exploratory"),
                    _option("functional", "Functional mechanism"),
                    _option("cohort-level", "Cohort-level prioritization"),
                ),
            ),
            TemplateField(
                key="inheritance_model",
                label="Inheritance model",
                kind="select",
                default="unknown",
                options=(
                    _option("unknown", "Unknown"),
                    _option("de-novo", "De novo"),
                    _option("dominant", "Dominant"),
                    _option("recessive", "Recessive"),
                    _option("compound-het", "Compound heterozygous"),
                    _option("x-linked", "X-linked"),
                ),
            ),
            TemplateField(
                key="affected_genes",
                label="Affected genes or loci",
                kind="textarea",
                placeholder="Optional known genes, intervals, panels, or candidate loci.",
            ),
        ),
        methodology=(
            "Annotate variants with deterministic sources/tools before interpretation.",
            "Separate pathogenicity evidence, functional plausibility, phenotype match, "
            "and inheritance fit.",
            "Rank variants using explicit evidence and uncertainty; do not rely on latent "
            "model knowledge alone.",
            "Avoid diagnostic claims beyond the evidence and call out needed validation.",
        ),
        report_guidance=(
            "Include a ranked variant/gene table with evidence categories and uncertainty.",
            "Summarize phenotype match, inheritance fit, functional rationale, "
            "and follow-up validation.",
            "Distinguish clinical interpretation from exploratory biological prioritization.",
        ),
        visualization_guidance=(
            "Use ranked tables as the primary output.",
            "Add gene/pathway summaries or locus diagrams only when supported by the data.",
        ),
    ),
)
