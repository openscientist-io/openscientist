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

- **Cite both:** For every curated model, parameter, or claim used in an analysis or report, cite the OpenADModels record path (for example `kb/models/michaels_2020/parameters/k_plus.yaml`) and its source publication (`PMCID` or `DOI`).
- **Prefer `override_sources`:** It maps a parameter id to the path of the record the value comes from (another model's parameter, or a parameter claim). The value is checked for units, rate-law form and process before it is applied. Raw `overrides` are applied unchecked.
- **Heed `check_parameter_swap`:** Run it before moving a value between models. If the verdict is not `compatible`, `compatible_after_conversion` or `zero_only`, the two papers mean different things by that constant: say so and do not apply it silently.

## 3. Check That a Model Can Be Simulated

- `list_models` and `get_model` report each model's `simulation_status`: `simulatable`, `needs_inputs` (pass `initial_values`), or `not_simulatable` (an analytical fit, a statistical model, or rate laws given only as prose), with `simulation_notes` saying why.
- Every result carries `warnings`. **`NOTHING CHANGES IN THIS SIMULATION` means the run produced no dynamics at all.** It is not evidence that a pathway is insensitive to a parameter. Report that the model could not be simulated and why; do not report a flat line as a finding.

## 4. Running Simulations and Batch Jobs

- **Single runs and comparisons:** `simulate` runs a curated model once; `compare_simulations` runs it as curated and with changed values, and returns the final-value differences and a plot.
- **Scans and sensitivity:** Do not loop `simulate`. Submit `submit_job(kind, spec, wait_seconds)` with `kind` `"scan"` (a grid of parameter values) or `"sensitivity"` (each parameter moved down and up), then `get_job(job_id)` until it finishes and `get_job_result(job_id)` for the summary, the plot and links to the CSV files. Give `duration` yourself when you can: left out, it is chosen from the unchanged model, and a changed value may need longer.
- **Budget:** A batch that would not finish inside its time limit returns `over_budget` with an estimate instead of running. Record the estimate and submit fewer conditions; do not resubmit the same job.
- **Failures:** A `failed` job says why (an unknown parameter or observable, a solver error). Fix the request rather than repeating it.
- **Models outside the library:** `simulate_sbml` runs any SBML or Antimony model, by BioModels id or as text. These models are **not curated here** and their values carry **no source snippets**; say so whenever you report them. An override of a species sets its starting concentration; a quantity fixed by an assignment rule cannot be overridden.

## 5. Sending Corrections Back

When you find a value, unit or citation in the library that the source paper contradicts, or something missing that a paper clearly states, `suggest_change(title, description, record_path, kind, proposed_yaml, submitted_by)` files it with the maintainers. Say what the source states and where (table, figure or paragraph).
