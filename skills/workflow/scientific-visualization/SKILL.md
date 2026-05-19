---
name: scientific-visualization
description: Choose figures that expose statistical evidence, uncertainty, and interpretation
category: workflow
tags:
  - visualization
  - reporting
  - figures
---

# Scientific Visualization

## Figure Selection

Pick figures that match the evidence object:

- Enrichment results: dot plot, bar chart, term table, enrichment map, or leading-member heatmap.
- Evidence synthesis: evidence matrix, study-quality table, effect-direction plot, or forest plot only when comparable quantitative effects exist.
- Simulations: trajectories, phase diagrams, parameter sweeps, scenario comparisons, and uncertainty bands for stochastic runs.
- Microbiome analysis: ordination, diversity summaries, differential abundance effect plots, and compositional caveat annotations.
- Variant interpretation: ranked variant/gene tables, phenotype-match summaries, inheritance views, or locus diagrams when supported.

## Required Figure Metadata

- Clear title that states the result being shown.
- Axis labels with units or transformed scales.
- Sample size, background universe, or tested count when relevant.
- Statistical threshold and multiple-testing correction when used.
- Direct labels for the most important marks instead of relying on dense legends.

## Guardrails

- Do not make decorative figures that add no evidence.
- Do not hide non-significant or failed results if they affect interpretation.
- Do not use a volcano, heatmap, forest plot, or network diagram unless the underlying data supports that geometry.
- If the figure is exploratory, label it as exploratory.
- Keep colors semantic and consistent across related figures.

## Reporting

Every figure should be accompanied by a caption that states:

1. what data was plotted,
2. what statistical method or transformation was used,
3. the main result,
4. the main limitation or uncertainty.
