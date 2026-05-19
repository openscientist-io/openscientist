---
name: ontology-enrichment
description: Statistically grounded ontology enrichment with explicit foreground/background sets
category: domain
tags:
  - enrichment
  - ontology
  - go
  - pathway
  - genomics
---

# Ontology Enrichment Analysis

## When to Use This Skill

Use this skill when a job asks for GO, phenotype ontology, pathway, or other
term enrichment over a foreground set against a background universe.

## Required Inputs

- Foreground set: genes, variants, taxa, or features being tested.
- Background universe: all assayed or eligible entities.
- Ontology or collection: GO, Reactome, KEGG, HPO, or a supplied term set.
- Identifier namespace and organism/build.

## Deterministic Analysis Steps

1. Normalize identifiers and report unmapped, duplicate, or ambiguous IDs.
2. Intersect foreground and background with the ontology annotation universe.
3. For each term, build a 2x2 contingency table over the background universe.
4. Use an appropriate enrichment test such as one-sided Fisher's exact or hypergeometric over-representation analysis.
5. Correct tested terms for multiple comparisons, preferably Benjamini-Hochberg FDR unless another method is requested.
6. Report term ID, term label, ontology namespace, overlap count, term size, background size, raw p-value, adjusted p-value, and driving foreground members.
7. Interpret biology only after deterministic statistics are available.

## Guardrails

- Do not infer enrichment from biological intuition alone.
- If no valid background was provided, state the bias and treat the result as exploratory.
- Avoid ranking purely by p-value when large generic terms dominate.
- Collapse redundant ontology terms during interpretation, but keep the statistical record intact.
- If the ontology is hierarchical, avoid presenting parent and child terms as independent mechanisms.

## Output Expectations

- A table of significant terms sorted by adjusted p-value.
- A short interpretation that separates statistical evidence from biological interpretation.
- A limitations note covering background universe, identifier mapping, annotation coverage, and database version if known.
