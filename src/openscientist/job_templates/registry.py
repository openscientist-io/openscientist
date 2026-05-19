"""Built-in guided job templates for OpenScientist."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from openscientist.job_templates.types import (
    JobTemplate,
    TemplateField,
    TemplateFieldOption,
    TemplateResolution,
    TemplateValidationError,
)

FREEFORM_TEMPLATE_ID = "freeform"
FREEFORM_DEFAULT_MAX_ITERATIONS = 10


def _option(value: str, label: str) -> TemplateFieldOption:
    return TemplateFieldOption(value=value, label=label)


def _text(inputs: Mapping[str, Any], key: str, fallback: str = "") -> str:
    value = inputs.get(key, fallback)
    return str(value).strip() if value is not None else fallback


def _build_gene_set_question(inputs: Mapping[str, Any]) -> str:
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


def _build_evidence_question(inputs: Mapping[str, Any]) -> str:
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


def _build_simulation_question(inputs: Mapping[str, Any]) -> str:
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


def _build_microbiome_question(inputs: Mapping[str, Any]) -> str:
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


def _build_variant_question(inputs: Mapping[str, Any]) -> str:
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


def _templates() -> tuple[JobTemplate, ...]:
    gene_set = JobTemplate(
        id="gene-set-enrichment",
        version="1",
        name="Gene Set Enrichment",
        summary="Guide enrichment analysis with foreground/background sets and database choice.",
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
        skill_slugs=("domain:genomics", "genomics", "domain:data-science", "data-science"),
        skill_categories=("workflow",),
        methodology=(
            "Run or design deterministic enrichment/statistical analysis before interpretation.",
            "Use the provided background set when available; if absent, state the bias this creates.",
            "Apply multiple-test correction and report the correction method.",
            "Do not infer enrichment from latent biological knowledge alone.",
        ),
        report_guidance=(
            "Include enriched terms, adjusted p-values/FDR, effect sizes or overlap counts, and driving genes.",
            "Separate statistical results from biological interpretation.",
            "Call out database choice and background-universe assumptions.",
        ),
        visualization_guidance=(
            "Prefer bar, dot, or enrichment-map style summaries of top significant terms.",
            "Show foreground/background overlap or leading genes when useful.",
        ),
        question_builder=_build_gene_set_question,
        default_max_iterations=2,
    )

    evidence = JobTemplate(
        id="evidence-synthesis",
        version="1",
        name="Evidence Synthesis",
        summary="Structure literature review, systematic evidence extraction, or meta-analysis planning.",
        fields=(
            TemplateField(
                key="research_focus",
                label="Research focus",
                kind="textarea",
                required=True,
                placeholder="What scientific relationship or claim should be evaluated?",
            ),
            TemplateField(
                key="population",
                label="Population or system",
                required=True,
                placeholder="e.g., adults with IBD, mouse gut microbiome, SARS-CoV-2 infections",
            ),
            TemplateField(
                key="exposure",
                label="Intervention, exposure, or comparison",
                placeholder="Optional treatment, condition, perturbation, or comparator.",
            ),
            TemplateField(
                key="outcome",
                label="Outcome measures",
                required=True,
                placeholder="e.g., remission rate, metabolite level, effect on pathway activity",
            ),
            TemplateField(
                key="evidence_sources",
                label="Evidence sources",
                kind="textarea",
                placeholder="Paste papers, PMIDs, search terms, or describe uploaded PDFs.",
            ),
            TemplateField(
                key="inclusion_criteria",
                label="Inclusion and exclusion criteria",
                kind="textarea",
                placeholder="Optional criteria for study type, dates, species, assay, or quality.",
            ),
            TemplateField(
                key="synthesis_mode",
                label="Synthesis mode",
                kind="select",
                required=True,
                default="evidence-synthesis",
                options=(
                    _option("evidence-synthesis", "Evidence synthesis"),
                    _option("scoping-review", "Scoping review"),
                    _option("meta-analysis-feasibility", "Meta-analysis feasibility"),
                    _option("quantitative-meta-analysis", "Quantitative meta-analysis"),
                ),
            ),
        ),
        skill_slugs=("domain:data-science", "data-science"),
        skill_categories=("workflow",),
        methodology=(
            "Separate evidence retrieval, study extraction, quality appraisal, and interpretation.",
            "Track study design, sample size, population/system, outcome definition, and direction of effect.",
            "Assess heterogeneity before making pooled or cross-study claims.",
            "Avoid overstating conclusions when evidence is sparse, indirect, or inconsistent.",
        ),
        report_guidance=(
            "Include an evidence table with study metadata and effect direction.",
            "Summarize confidence, limitations, and gaps separately from biological interpretation.",
            "Use citations with PMID links where available.",
        ),
        visualization_guidance=(
            "Use evidence matrices or effect-direction summaries.",
            "Only create forest plots when comparable quantitative effects are available.",
        ),
        question_builder=_build_evidence_question,
        default_max_iterations=4,
    )

    simulation = JobTemplate(
        id="computational-simulation",
        version="1",
        name="Computational Simulation",
        summary="Set up simulations with explicit assumptions, parameters, perturbations, and observables.",
        fields=(
            TemplateField(
                key="system_model",
                label="System or model",
                required=True,
                placeholder="e.g., predator-prey dynamics, metabolic pathway, diffusion process",
            ),
            TemplateField(
                key="model_type",
                label="Model type",
                kind="select",
                required=True,
                default="custom",
                options=(
                    _option("ode", "ODE / dynamical system"),
                    _option("stochastic", "Stochastic simulation"),
                    _option("agent-based", "Agent-based model"),
                    _option("network", "Network model"),
                    _option("physics", "Physics-based model"),
                    _option("custom", "Custom / decide from context"),
                ),
            ),
            TemplateField(
                key="parameters",
                label="Parameters and initial conditions",
                kind="textarea",
                required=True,
                placeholder="List known parameters, ranges, units, and initial conditions.",
                rows=4,
            ),
            TemplateField(
                key="perturbations",
                label="Perturbations to test",
                kind="textarea",
                required=True,
                placeholder="What interventions, parameter changes, or scenarios should be simulated?",
            ),
            TemplateField(
                key="observables",
                label="Outputs to measure",
                kind="textarea",
                required=True,
                placeholder="What model outputs should be tracked and compared?",
            ),
            TemplateField(
                key="assumptions",
                label="Assumptions",
                kind="textarea",
                placeholder="Known simplifications, constraints, or theoretical assumptions.",
            ),
        ),
        skill_slugs=("domain:data-science", "data-science"),
        skill_categories=("workflow",),
        methodology=(
            "State model assumptions, equations or update rules, and parameter units before running simulations.",
            "Run sensitivity analysis or parameter sweeps for important uncertain inputs.",
            "Distinguish simulation predictions from empirical evidence.",
            "Check whether results are robust to numerical settings and plausible parameter ranges.",
        ),
        report_guidance=(
            "Include parameter tables, assumptions, scenarios tested, and sensitivity results.",
            "Explain model behavior mechanistically, including failure modes and uncertainty.",
            "Do not present simulated outcomes as validated biological facts.",
        ),
        visualization_guidance=(
            "Plot trajectories, phase diagrams, parameter sweeps, or scenario comparisons as appropriate.",
            "Use uncertainty bands or replicate summaries for stochastic simulations.",
        ),
        question_builder=_build_simulation_question,
        default_max_iterations=4,
    )

    microbiome = JobTemplate(
        id="microbiome-differential-abundance",
        version="1",
        name="Microbiome Differential Abundance",
        summary="Analyze microbiome feature differences with metadata, covariates, and compositional caveats.",
        fields=(
            TemplateField(
                key="comparison",
                label="Comparison",
                required=True,
                placeholder="e.g., responders vs non-responders, treatment vs control",
            ),
            TemplateField(
                key="feature_table_description",
                label="Feature table or uploaded data",
                kind="textarea",
                placeholder="Describe ASV/OTU/taxon table, counts, relative abundance, or uploaded files.",
            ),
            TemplateField(
                key="metadata_description",
                label="Sample metadata",
                kind="textarea",
                required=True,
                placeholder="Describe metadata columns, sample groups, batches, and collection context.",
            ),
            TemplateField(
                key="grouping_variable",
                label="Primary grouping variable",
                required=True,
                placeholder="e.g., treatment_group",
            ),
            TemplateField(
                key="covariates",
                label="Covariates",
                placeholder="Optional confounders such as batch, age, sex, site, diet, sequencing depth.",
            ),
            TemplateField(
                key="taxonomic_level",
                label="Taxonomic level",
                kind="select",
                required=True,
                default="auto",
                options=(
                    _option("auto", "Auto-select"),
                    _option("asv-otu", "ASV/OTU"),
                    _option("species", "Species"),
                    _option("genus", "Genus"),
                    _option("family", "Family"),
                    _option("phylum", "Phylum"),
                ),
            ),
        ),
        skill_slugs=("domain:data-science", "data-science", "domain:genomics", "genomics"),
        skill_categories=("workflow",),
        methodology=(
            "Inspect sample balance, missing metadata, sequencing depth, and feature sparsity before testing.",
            "Account for microbiome compositionality and multiple testing.",
            "Separate differential abundance from causal or functional claims.",
            "Check confounders and batch effects before interpreting taxa biologically.",
        ),
        report_guidance=(
            "Include QC, alpha/beta diversity where applicable, differential taxa, effect direction, and FDR.",
            "Report the taxonomic level, normalization/transformation, and statistical method.",
            "State limitations around compositionality, sample size, and confounding.",
        ),
        visualization_guidance=(
            "Use ordination, diversity summaries, abundance plots, and volcano-style summaries where appropriate.",
            "Make figures interpretable at the selected taxonomic level.",
        ),
        question_builder=_build_microbiome_question,
        default_max_iterations=3,
    )

    variant = JobTemplate(
        id="variant-interpretation",
        version="1",
        name="Variant Interpretation",
        summary="Rank variants with phenotype context, inheritance assumptions, and deterministic annotation first.",
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
                placeholder="Clinical phenotype, disease, traits, HPO terms, model organism phenotype, or cohort context.",
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
        skill_slugs=("domain:genomics", "genomics", "domain:data-science", "data-science"),
        skill_categories=("workflow",),
        methodology=(
            "Annotate variants with deterministic sources/tools before interpretation.",
            "Separate pathogenicity evidence, functional plausibility, phenotype match, and inheritance fit.",
            "Rank variants using explicit evidence and uncertainty; do not rely on latent model knowledge alone.",
            "Avoid diagnostic claims beyond the evidence and call out needed validation.",
        ),
        report_guidance=(
            "Include a ranked variant/gene table with evidence categories and uncertainty.",
            "Summarize phenotype match, inheritance fit, functional rationale, and follow-up validation.",
            "Distinguish clinical interpretation from exploratory biological prioritization.",
        ),
        visualization_guidance=(
            "Use ranked tables as the primary output.",
            "Add gene/pathway summaries or locus diagrams only when supported by the data.",
        ),
        question_builder=_build_variant_question,
        default_max_iterations=3,
    )

    return (gene_set, evidence, simulation, microbiome, variant)


_TEMPLATES: dict[str, JobTemplate] = {template.id: template for template in _templates()}


def normalize_template_id(template_id: str | None) -> str | None:
    """Normalize empty/freeform identifiers to None."""
    if template_id is None:
        return None
    normalized = template_id.strip()
    if not normalized or normalized == FREEFORM_TEMPLATE_ID:
        return None
    return normalized


def list_job_templates() -> list[JobTemplate]:
    """Return built-in guided templates in display order."""
    return list(_TEMPLATES.values())


def get_job_template(template_id: str | None) -> JobTemplate | None:
    """Return a built-in template, or None for freeform jobs."""
    normalized = normalize_template_id(template_id)
    if normalized is None:
        return None
    return _TEMPLATES.get(normalized)


def get_job_template_or_raise(template_id: str) -> JobTemplate:
    """Return a template or raise a validation error for unknown IDs."""
    template = get_job_template(template_id)
    if template is None:
        raise TemplateValidationError(f"Unknown job template: {template_id}.")
    return template


def default_max_iterations_for_template(template_id: str | None) -> int:
    """Return the suggested iteration count for a workflow selector value."""
    template = get_job_template(template_id)
    if template is None:
        return FREEFORM_DEFAULT_MAX_ITERATIONS
    return template.default_max_iterations


def workflow_options() -> dict[str, str]:
    """Return selector options for the new-job UI."""
    return {FREEFORM_TEMPLATE_ID: "Freeform"} | {
        template.id: template.name for template in list_job_templates()
    }


def parse_template_inputs(value: Any) -> dict[str, Any] | None:
    """Parse template input payloads from JSON, form strings, or dictionaries."""
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError("template_inputs must be a JSON object.") from exc
        if parsed is None:
            return None
        if not isinstance(parsed, dict):
            raise ValueError("template_inputs must be a JSON object.")
        return parsed
    raise ValueError("template_inputs must be a JSON object.")


def resolve_template_submission(
    *,
    template_id: str | None,
    template_inputs: Mapping[str, Any] | None,
    research_question: str | None,
) -> TemplateResolution:
    """Validate template inputs and produce the effective research question."""
    normalized_template_id = normalize_template_id(template_id)
    question = (research_question or "").strip()

    if normalized_template_id is None:
        if not question:
            raise TemplateValidationError("Please enter a research question.")
        return TemplateResolution(
            template_id=None,
            template_version=None,
            template_inputs=None,
            research_question=question,
            agent_guidance=None,
        )

    template = get_job_template_or_raise(normalized_template_id)
    normalized_inputs = template.validate_inputs(template_inputs)
    effective_question = question or template.build_research_question(normalized_inputs)
    if not effective_question:
        raise TemplateValidationError(f"{template.name} could not generate a research question.")

    return TemplateResolution(
        template_id=template.id,
        template_version=template.version,
        template_inputs=normalized_inputs,
        research_question=effective_question,
        agent_guidance=template.build_agent_guidance(normalized_inputs),
    )


def build_template_agent_guidance(
    template_id: str | None,
    template_inputs: Mapping[str, Any] | None,
) -> str | None:
    """Build runtime guidance for a persisted template submission."""
    normalized_template_id = normalize_template_id(template_id)
    if normalized_template_id is None:
        return None
    template = get_job_template(normalized_template_id)
    if template is None:
        return None
    normalized_inputs = template.validate_inputs(template_inputs)
    return template.build_agent_guidance(normalized_inputs)


def build_template_research_question(
    template_id: str | None,
    template_inputs: Mapping[str, Any] | None,
) -> str:
    """Generate the default research question for a template."""
    normalized_template_id = normalize_template_id(template_id)
    if normalized_template_id is None:
        return ""
    template = get_job_template_or_raise(normalized_template_id)
    normalized_inputs = template.validate_inputs(template_inputs)
    return template.build_research_question(normalized_inputs)


def filter_skills_for_template(skills: Iterable[Any], template_id: str | None) -> list[Any]:
    """Return the skills that should be written for a selected template.

    Freeform jobs keep current behavior and receive all enabled skills. Guided
    jobs always receive workflow skills plus template-matched domain skills.
    """
    template = get_job_template(template_id)
    skill_list = list(skills)
    if template is None:
        return skill_list

    selected: list[Any] = []
    selected_keys: set[tuple[str, str]] = set()
    for skill in skill_list:
        if _skill_matches_template(skill, template):
            key = (str(getattr(skill, "category", "")), str(getattr(skill, "slug", "")))
            if key not in selected_keys:
                selected.append(skill)
                selected_keys.add(key)
    return selected


def _skill_matches_template(skill: Any, template: JobTemplate) -> bool:
    category = str(getattr(skill, "category", "") or "")
    slug = str(getattr(skill, "slug", "") or "")
    name = str(getattr(skill, "name", "") or "").lower()
    tags = getattr(skill, "tags", []) or []
    tag_values = {str(tag).lower() for tag in tags}

    if category == "workflow":
        return True
    if category in template.skill_categories:
        return True
    if slug in template.skill_slugs:
        return True
    if f"{category}:{slug}" in template.skill_slugs:
        return True

    template_terms = set(template.skill_slugs) | set(template.skill_categories)
    normalized_terms = {term.split(":", 1)[-1].replace("-", " ") for term in template_terms}
    return bool(tag_values.intersection(template_terms)) or any(
        term in name for term in normalized_terms
    )
