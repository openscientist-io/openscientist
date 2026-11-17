---
name: openadmodels
description: Curated quantitative Alzheimer's-disease models, parameters, and simulation tools via the OpenADModels MCP server
category: domain
---

# OpenADModels Quantitative Modeling Guide

OpenADModels is a curated library of quantitative Alzheimer's-disease (QSP and PBPK) models, parameters, and literature claims exposed through the `openadmodels` MCP server. Every curated parameter and claim traces to a verbatim snippet in its source paper.

## 1. Search the Library Before Simulating

Inspect existing models, parameters, ontology terms, and literature claims before running any simulation:

- `search_kb`, `get_record`: Search curated literature claims and parameter records, then inspect the full record and its verbatim source snippet.
- `list_models`, `get_model`: Browse available QSP/PBPK models and inspect compartments, species, reactions, default parameters, and observables.
- `find_parameters`, `check_parameter_swap`: Locate candidate parameter records and check unit, species, compartment, and biological compatibility before swapping a value into a model.
- `ontology_search`, `ontology_term`: Resolve biological entities and terms to canonical ontology identifiers.
- `biomodels_search`: Search BioModels when no curated model covers the pathway of interest.

## 2. Provenance and Parameter Overrides

- **Cite both IDs:** For every curated model, parameter, or claim used in an analysis or report, cite both the OpenADModels record ID and its source publication identifier (`PMCID` or `DOI`).
- **Prefer `override_sources`:** Pass cited OpenADModels record IDs via `override_sources` instead of raw numeric overrides whenever a curated record exists.
- **Heed `check_parameter_swap`:** Run `check_parameter_swap` before applying a parameter from another study or context. If it flags unit, species, or context mismatches, respect the warning and document any limitation.

## 3. Running Simulations and Batch Jobs

- **Single runs and comparisons:** Use `simulate` for a single curated-model run and `compare_simulations` to compare baseline and perturbed conditions (returns JSON metrics plus a PNG plot).
- **Scans, sensitivity, and long jobs:** Never loop `simulate` across parameter grids or perturbations. Submit a batch job with `submit_job(kind, spec, wait_seconds)` using `kind` in `"simulate"`, `"scan"`, `"sensitivity"`, or `"sbml"`, then poll or retrieve outputs with `get_job(job_id)` and `get_job_result(job_id)` (`list_jobs()` and `cancel_job(job_id)` manage active jobs).
- **Uncurated SBML (`simulate_sbml`):** `simulate_sbml` runs arbitrary SBML or Antimony models (by BioModels ID or model text). Results from `simulate_sbml` (and `"sbml"` jobs) are **not curated** and carry **no verbatim source snippets**; state this explicitly whenever reporting them.

## 4. Interpreting Outcomes and Filing Suggestions

- **Record non-change and budget outcomes as findings:**
  - `over_budget`: A batch job that exceeds its time budget returns status `over_budget` with a runtime estimate instead of running. Record the estimate and narrow the spec if needed; do not retry the identical job blindly.
  - `failed`: Record the failure mode and numerical/biological reason rather than repeating the same call.
  - `"NOTHING CHANGES"`: Invariance of an output to a parameter or perturbation is a mechanistic negative finding. Record it as evidence about pathway insensitivity rather than treating it as a broken run.
- **Library feedback:** When you identify a missing paper value or curation correction with clear provenance, you may file a maintainer suggestion with `suggest_change(title, description, record_path, kind, proposed_yaml, submitted_by)`.
