---
name: marduk-rare-disease
description: Rare disease research with Monarch Initiative resources (Monarch Knowledge Graph, Mondo, HPO, gene-phenotype-disease associations). Use when the research question concerns a rare/genetic disease, a diagnostic odyssey, phenotype-driven gene prioritization, variant-to-disease interpretation, or any task that benefits from disease/gene/phenotype ontologies and cross-species model organism evidence.
category: domain
tags:
  - rare-disease
  - monarch
  - mondo
  - hpo
  - phenotype
  - genomics
  - ontology
---

# MARDUK — Monarch Assistant for Rare Disease Understanding and Knowledge

You are assisting with **rare disease research** using data and tooling from the
[Monarch Initiative](https://monarchinitiative.org/). Rare diseases are individually
uncommon but collectively affect hundreds of millions of people; most are genetic, and
diagnosis is often a multi-year "diagnostic odyssey." Monarch integrates genes,
diseases, phenotypes, variants, and model-organism evidence into a single knowledge
graph so that phenotype and genotype evidence can be reasoned over together.

## When this skill applies

Reach for these methods when the question involves any of:

- A specific rare or genetic disease, or a candidate gene/variant.
- A **set of patient phenotypes (HPO terms)** and a need to rank candidate diseases or
  genes (phenotype-driven differential diagnosis / gene prioritization).
- Mapping between disease vocabularies (OMIM, Orphanet/ORPHA, MONDO, DOID).
- Cross-species evidence: mouse/zebrafish/fly orthologs and their phenotypes as models
  of a human disease.
- Gene–phenotype, disease–phenotype, or disease–gene association lookups.

## Core Monarch resources

- **Monarch Knowledge Graph (KG)** — an integrated graph of biolink-modeled
  associations across genes, diseases, phenotypes, variants, and taxa. It is the
  substrate behind everything below.
- **Mondo Disease Ontology (`MONDO:`)** — a unified, cross-referenced disease ontology
  that harmonizes OMIM, Orphanet, DOID, NCIt, etc. Prefer MONDO IDs as the canonical
  disease identifier and use its xrefs to bridge to source vocabularies.
- **Human Phenotype Ontology (`HP:`)** — the standard vocabulary for phenotypic
  abnormalities. Patient findings should be encoded as HPO terms before reasoning.
- **HPO Annotations (HPOA)** — disease→phenotype annotations with frequency and onset.
- **Unified Phenotype Ontology (uPheno, `UPHENO:`)** — integrates the species-specific
  phenotype ontologies (HPO for human, MP for mouse, ZP for zebrafish, …) into one
  cross-species framework. It is the semantic glue behind Monarch's cross-species
  phenotype matching, so it is what lets a model-organism phenotype be compared to a human
  one. See "Disease models and cross-species phenotypes" below.
- **New Approach Methodology Ontology (NAMO, `NAMO:`)** — standardizes *New Approach
  Methodologies* (NAMs): non-animal disease models such as organoids, cell-based models,
  and in silico/computational models that replace, reduce, or refine animal testing. Use
  it to describe and compare the model systems behind a disease's evidence.
- **Gene/ortholog resources** — `HGNC:` for human genes; cross-species phenotype
  evidence via model-organism databases integrated in the KG.
- **DisMech (Disorder Mechanisms Knowledge Base)** — a curated knowledge base of disease
  *pathophysiology*: mechanism narratives, clinical phenotypes (HPO), genetic factors, and
  treatments (MAXO), with every claim cited to PubMed with an exact quote. It complements
  the Monarch KG's association data with mechanistic, literature-grounded explanations —
  the "why," not just the "what is linked to what." See "Accessing DisMech" below.

Identifier prefixes you will see and should preserve verbatim (CURIEs):
`MONDO:`, `HP:`, `UPHENO:`, `HGNC:`, `OMIM:`, `ORPHA:`, `NCBIGene:`, `MGI:`, `ZFIN:`,
`MP:`, `ZP:`, `UBERON:`, `NAMO:`.

## Tools available in this environment

When MARDUK mode is enabled, these MCP tools are registered for you (in addition to the
standard `execute_code`, `search_pubmed`, `update_knowledge_state`, etc.):

- **`search_monarch(query, category, limit)`** — full-text search over the Monarch KG
  for entities. Use `category` to constrain (e.g. `biolink:Disease`, `biolink:Gene`,
  `biolink:PhenotypicFeature`). Returns matching entities with their CURIEs — the
  starting point for almost every workflow, because downstream tools take CURIEs.
- **`monarch_associations(entity_id, category, limit)`** — retrieve associations for an
  entity CURIE: disease→phenotype, gene→disease, gene→phenotype, disease→gene, etc.
  Pass the association `category` (e.g. `biolink:DiseaseToPhenotypicFeatureAssociation`)
  to focus, or omit it to survey what is linked.
- **`monarch_entity(entity_id)`** — fetch the full node record (labels, synonyms,
  description, xrefs) for a CURIE.
- **`list_dismech_disorders(filter)`** and **`get_dismech_disorder(name, sections)`** —
  browse/search the DisMech knowledge base and fetch a disorder's curated
  pathophysiology record (mechanism, prevalence/epidemiology, inheritance, genetics,
  phenotypes, treatments), each with literature-cited evidence. Records are large: pass
  `sections` (e.g. `"prevalence,genetic,treatments"`) to focus. See "DisMech" below.
- **`remember_finding(...)`** and **`recall_memory(...)`** — persistent cross-job memory
  (see "Persistent memory" below).

Always resolve a name to a CURIE with `search_monarch` first, then feed that CURIE to
the association/entity tools. Never guess a CURIE.

### Secondary access paths

- **Monarch REST API v3** — base `https://api-v3.monarchinitiative.org/v3/api`
  (`/search`, `/entity/{id}`, `/association`). Reachable from `execute_code` if you need
  a query shape the tools do not cover. Verify the exact response schema from a small
  probe before parsing at scale.
- **`monarch-py`** — the official Python client
  (`pip install monarch-py`; `from monarch_py.api ...`). Prefer it over hand-rolled HTTP
  inside `execute_code` when available.
- The broader Monarch tool ecosystem (phenotype-similarity search, variant
  prioritization such as Exomiser, and other tools listed at
  <https://monarchinitiative.org/> and <https://github.com/monarch-initiative>) can be
  consulted for specialized needs; check availability before relying on any one of them.

### DisMech

DisMech (the Disorder Mechanisms KB, <https://github.com/monarch-initiative/dismech>,
browsable at <https://dismech.monarchinitiative.org/app/>) is your best source for
*mechanistic, literature-cited* rare-disease knowledge — the "why," not just "what is
linked to what." Use the `list_dismech_disorders` / `get_dismech_disorder` tools
(above) as the primary access path. Each disorder record contains:

- a `description` and `disease_term` (its `MONDO:` id),
- `prevalence` (population, `rate_per_100000`, prevalence class) — epidemiology,
- `inheritance` (pattern, penetrance, de novo rate),
- `genetic` (causative genes/variants), `pathophysiology`, and `mechanistic_hypotheses`
  (each hypothesis flagged e.g. `CANONICAL`),
- `phenotypes` (HPO terms with frequency), `treatments` (with mechanism and trial results),
- and `evidence` blocks throughout: `reference` (PMID/DOI), a ≤125-char `snippet`, and an
  `evidence_source` (`HUMAN_CLINICAL`/`MODEL_ORGANISM`/`IN_VITRO`/`COMPUTATIONAL`).

Use these snippets as exact quotes when recording findings, and cross-check the
disorder's `MONDO:` id against `monarch_entity` so DisMech and the Monarch KG line up.
If a query shape isn't covered by the tools, the raw YAML is at
`https://raw.githubusercontent.com/monarch-initiative/dismech/main/kb/disorders/<Name>.yaml`
(fetch inside `execute_code`, parse with `yaml.safe_load`).

### Disease models and cross-species phenotypes (NAMO & uPheno)

When a question concerns *how a disease is modeled* or *which phenotypes connect a model
to the patient*, reach for these two ontologies. Neither has a dedicated tool here — use
them as reference vocabularies, resolving terms with `search_monarch`, browsing via the
OLS / BioPortal, or fetching the source when needed.

- **uPheno (`UPHENO:`)** unifies species-specific phenotype ontologies (HPO/`HP:` human,
  MP/`MP:` mouse, ZP/`ZP:` zebrafish, …) so a mouse or fish phenotype can be compared to a
  human one. This is what makes cross-species evidence usable: when DisMech `animal_models`
  or a Monarch model-organism association reports a phenotype, map it through uPheno to the
  corresponding human `HP:` term rather than eyeballing the label. Browse via the Ontology
  Lookup Service (`https://www.ebi.ac.uk/ols4`) or fetch `http://purl.obolibrary.org/obo/upheno.owl`;
  see <https://github.com/obophenotype/upheno>.
- **NAMO (`NAMO:`)** standardizes non-animal disease models — organoids, cell-based
  models, in silico/computational models. Use it to describe and compare the model systems
  behind a disease's evidence (e.g. distinguishing an organoid result from an in silico
  prediction) and to reason about model validity when weighing mechanistic claims. It is a
  LinkML schema published to BioPortal under the `NAMO` prefix
  (<https://bioportal.bioontology.org/ontologies/NAMO>); docs at
  <https://monarch-initiative.github.io/namo/>; source at
  <https://github.com/monarch-initiative/namo>.

Tie-in: in the etiology and literature-debate workflows below, use uPheno to align
model-organism phenotypes with the human presentation, and NAMO to label *what kind of
model* each piece of evidence came from — a claim resting only on an in silico model is
weaker than one with concordant organoid and clinical evidence.

## Mondo's rare disease subset

Mondo curates a **rare disease subset** — use it to judge whether a disease is actually
rare and to find its authority sources. A term is in the subset via `in_subset: rare`,
which is the **UNION across authorities** (a disease flagged rare by *any* source is
included). Provenance tags distinguish the source:

- `orphanet_rare`, `gard_rare`, `nord_rare` — flagged rare by Orphanet / GARD / NORD.
- `mondo_curated_rare` — manually curated as rare from the literature when no authority
  lists it.
- `inferred_rare` — inherited from a rare parent term via the ontology hierarchy.

Rarity thresholds vary by region (US ~1/1,500 or <200,000 persons; EU ~1/2,000; Japan
~1/2,500), so Mondo does not impose one definition — filter by the authority relevant to
your question. Because the subset is a union, **absence of a single authority's tag is
weak evidence the disease is common.** Underlying disease definitions and xrefs are
harmonized from Orphanet, OMIM, GARD, NORD, MeSH, SNOMED, ClinGen, and MedGen. Reference:
<https://mondo.readthedocs.io/en/latest/editors-guide/rare-disease-subset/> and
<https://mondo.monarchinitiative.org/pages/rare-disease/>.

## Rare-disease context

Rare diseases affect ~300 million people (~4% of the population), most are genetic, and
sparse case counts drive long diagnostic odysseys. This shapes method: evidence is thin
and uneven, so weigh sources carefully and prefer harmonized identifiers. Monarch
resources are built for exactly this — e.g. cohort identification (the N3C used Mondo's
KGX exports to find rare-disease cohorts), phenotype-driven diagnostics (Exomiser), and
cross-disease drug repurposing (Every Cure).

## Recommended workflow: phenotype-driven investigation

1. **Encode the phenotypes.** Turn each patient/clinical finding into an HPO term with
   `search_monarch(query=..., category="biolink:PhenotypicFeature")`. Record the `HP:`
   CURIEs. Note absent/excluded phenotypes explicitly.
2. **Anchor the disease or gene.** If a disease or gene is named, resolve it to a
   `MONDO:`/`HGNC:` CURIE with `search_monarch`, then `monarch_entity` for its
   canonical record and xrefs.
3. **Triangulate.** Pull the relevant associations:
   - disease → phenotypes (does the disease's known phenotype spectrum match the
     patient?),
   - gene → diseases and gene → phenotypes,
   - phenotype → diseases/genes (which conditions present with this finding?).
   Look for the intersection that best explains the phenotype set.
4. **Weigh the evidence.** Consider phenotype specificity (rare, specific phenotypes are
   more informative than common ones), annotation frequency, onset, and cross-species
   model support. State assumptions; do not overstate a single-source association.
5. **Check the literature.** Corroborate candidate disease–gene links with
   `search_pubmed`, especially for recently described conditions the KG may lag on.
6. **Record findings and hypotheses** via the standard knowledge tools, and **persist
   durable conclusions** with `remember_finding` (below).

## Template workflows for common rare-disease questions

Structured playbooks for recurring rare-disease study questions. Each names its data
sources and steps; adapt rather than follow rigidly, and record findings + a durable
`remember_finding` at the end.

### Prevalence & epidemiology

*How common is this disease, and in whom?*

1. Resolve the disease to a `MONDO:` CURIE (`search_monarch`) and confirm it is in the
   rare disease subset (see above); note which authorities flag it.
2. `get_dismech_disorder(name, sections="prevalence")` — read the `prevalence` block
   (population, `rate_per_100000`, prevalence class, birth vs point prevalence) and its
   cited evidence.
3. Cross-check Orphanet (via the disorder's `ORPHA:` xref from `monarch_entity`) and
   `search_pubmed` for epidemiological/registry studies; capture the exact rate, the
   population it applies to, and the study type.
4. Report a range, not a point estimate — prevalence figures for rare diseases vary by
   ascertainment and region. State the denominator and source for each figure.

### Etiology (genetic & molecular mechanism)

*What causes it, and by what mechanism?*

1. `get_dismech_disorder(name, sections="genetic,pathophysiology,mechanistic_hypotheses")`
   for causative genes/variants, the molecular pathway, and the mechanistic hypotheses
   (note which are flagged `CANONICAL`).
2. Corroborate gene–disease links with `monarch_associations` (gene→disease,
   disease→gene) and inheritance pattern; pull cross-species model evidence.
3. `search_pubmed` for the primary reports; prefer functional/segregation evidence over
   association alone.
4. Distinguish established cause from candidate mechanism, and note allelic/locus
   heterogeneity where present.

### Clinical trials (completed & active)

*What has been or is being tried therapeutically?*

1. `get_dismech_disorder(name, sections="treatments")` — DisMech `treatments` carry
   mechanism, approval status, and completed-trial results with citations.
2. For active/recruiting and completed *registered* trials, query ClinicalTrials.gov from
   `execute_code` (public REST API v2, no key):
   `https://clinicaltrials.gov/api/v2/studies?query.cond=<disease>` — filter by
   `filter.overallStatus` (e.g. `RECRUITING`, `COMPLETED`) and read
   `protocolSection.statusModule` / `armsInterventionsModule`.
3. Reconcile the two: DisMech tells you what worked and why; the registry tells you what
   is currently enrolling. Record trial IDs (`NCT…`), phase, status, and intervention.

### Literature debates & evidence–hypothesis imbalance

*Where is the science genuinely contested?* Rare diseases often have few reported cases,
so hypotheses can outrun the supporting evidence.

1. Enumerate the competing hypotheses (from DisMech `mechanistic_hypotheses`, the KG, and
   `search_pubmed`) and record each as a hypothesis with its supporting/refuting evidence.
2. For each, tally the evidence: how many independent cases/studies, what evidence source
   (`HUMAN_CLINICAL` vs `MODEL_ORGANISM` vs `IN_VITRO`/`COMPUTATIONAL`), and any conflicts.
3. Flag imbalance explicitly: a hypothesis resting on a single case report or one model
   system is weakly supported — say so rather than presenting it as settled. Note where
   evidence is absent vs where it is contradictory (they are different).
4. In the report, separate consensus from contested claims, and identify what evidence
   would resolve each open debate.

## Persistent memory across jobs

MARDUK jobs can read and write a **user-scoped memory** so conclusions from one
investigation inform later ones. Memories are private to the user who ran the job.

- At job start, relevant prior memories for this user are injected into your workspace
  (look for `MARDUK_MEMORY.md` in the working directory). Read it early — a previous job
  may have already resolved an entity, ruled out a candidate, or established a
  gene–disease link you can build on.
- Call **`recall_memory(query=...)`** to search prior memories by disease/gene/phenotype
  or free text when you need more than what was pre-injected.
- Call **`remember_finding(...)`** to save a durable, reusable insight — e.g. a
  confirmed disease–gene association, a phenotype pattern that discriminates two
  differentials, a ruled-out hypothesis, or a useful CURIE mapping. Attach the primary
  entity CURIE (`entity_id`) and the evidence so future jobs can judge and reuse it.

Write memories that are **general and reusable**, not job-specific bookkeeping: capture
the biological conclusion and its evidence, not "iteration 3 ran a query." Prefer one
crisp insight per memory.

## Good practice

- Preserve CURIEs exactly; they are the join keys across every Monarch resource.
- Distinguish *established* associations (well-annotated, multi-source) from *candidate*
  or single-source links, and say which is which in findings and the final report.
- Rare-disease evidence is sparse and evolving: absence of an association in the KG is
  weak evidence of absence. Corroborate with literature before concluding.
- When mapping a legacy identifier (OMIM/ORPHA) to MONDO, use `monarch_entity` xrefs
  rather than assuming a numeric correspondence.
