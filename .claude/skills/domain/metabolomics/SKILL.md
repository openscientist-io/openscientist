---
name: metabolomics
description: Metabolomics-specific analysis strategies and domain knowledge
---

# Metabolomics Analysis

## When to Use This Skill

- When data contains metabolite measurements
- When analyzing metabolic pathways or fluxes
- When interpreting biochemical mechanisms

## Core Concepts

### Metabolite Naming

Metabolites have multiple naming conventions:
- **IUPAC names**: Chemical nomenclature (e.g., "2-aminoethanesulfonic acid")
- **Common names**: Biology names (e.g., "Taurine")
- **Abbreviations**: Shorthand (e.g., "Tau")

**Always verify metabolite identity** before interpreting results.

### Pathway Context

Metabolites exist in biochemical pathways:
- **Substrates** → **Enzymes** → **Products**
- Changes in one metabolite affect connected metabolites
- Pathway analysis is more informative than individual metabolites

**Example pathway:**
```
Glucose → (HK) → G6P → (G6PDH) → 6PG
```
If G6P ↑ and 6PG unchanged → suggests bottleneck at G6PDH enzyme

### Flux vs Concentration

**Concentration**: Amount of metabolite present
**Flux**: Rate of metabolite conversion

**Key insight:**
- High concentration + low downstream product = bottleneck (slow flux)
- Low concentration + high downstream product = high flux
- Calculate flux proxies using ratios: Product/Substrate

### Common Metabolomics Patterns

**Pattern 1: Substrate Depletion**
```
Precursor ↓↓, Product ↑↑
→ Interpretation: Active consumption, increased flux
```

**Pattern 2: Bottleneck**
```
Substrate ↑↑, Product ↓↓ or unchanged
→ Interpretation: Enzymatic bottleneck, blocked conversion
```

**Pattern 3: Pathway Shutdown**
```
All pathway metabolites ↓↓
→ Interpretation: Reduced pathway activity
```

**Pattern 4: Salvage vs De Novo**
```
De novo intermediates ↓, Salvage products ↑
→ Interpretation: Metabolic shift to energy-efficient salvage
```

## Analysis Strategies

### 1. Pathway Enrichment

**When:** You have many differentially abundant metabolites

**How:**
```python
# Group metabolites by pathway
pathway_metabolites = {
    "Glycolysis": ["Glucose", "G6P", "F6P", "FBP", ...],
    "TCA Cycle": ["Citrate", "Isocitrate", "α-KG", ...],
    "Purine Metabolism": ["AMP", "ADP", "ATP", "IMP", ...]
}

# Count hits per pathway
for pathway, metabolites in pathway_metabolites.items():
    hits = [m for m in significant_metabolites if m in metabolites]
    enrichment_score = len(hits) / len(metabolites)
```

**Resources:**
- KEGG pathways: https://www.genome.jp/kegg/pathway.html
- BioCyc: https://biocyc.org/

### 2. Flux Index Calculation

**When:** You want to infer enzymatic activity

**How:**
```python
# Simple flux proxy: Product / Substrate
flux_index = data["Product"] / data["Substrate"]

# Compare across groups
t_test(flux_index[group1], flux_index[group2])
```

**Common indices:**
- **Glycolysis flux**: FBP / G6P
- **TCA flux**: Citrate / Acetyl-CoA (if available)
- **Salvage flux**: Product / Precursor

### 3. Energy Charge Calculation

**When:** Assessing cellular energy state

**Formula:**
```python
# Adenylate energy charge
AEC = (ATP + 0.5*ADP) / (ATP + ADP + AMP)
# Range: 0 (depleted) to 1 (high energy)

# Similar for GTP, CTP, UTP
```

**Interpretation:**
- AEC > 0.8: High energy state
- AEC < 0.5: Energy crisis

### 4. Redox State Assessment

**When:** Investigating oxidative stress or metabolic state

**Ratios:**
```python
NAD_ratio = NAD+ / NADH  # High = oxidized state
NADP_ratio = NADP+ / NADPH  # High = oxidative stress
GSH_ratio = GSH / GSSG  # Low = oxidative stress
```

## Metabolomics-Specific Hypotheses

### Template Hypotheses

**H1: Pathway Shift Hypothesis**
```
"Condition X shifts metabolism from [pathway A] to [pathway B]
due to [mechanism], evidenced by [metabolite pattern]"
```

**H2: Enzymatic Bottleneck Hypothesis**
```
"Enzyme [E] activity is reduced in condition X, causing accumulation
of substrate [S] and depletion of product [P]"
```

**H3: Cofactor Limitation Hypothesis**
```
"Limited availability of cofactor [C] constrains pathway [P],
causing metabolite pattern [M]"
```

**H4: Energy State Hypothesis**
```
"Condition X induces low-energy state, triggering metabolic
reprogramming to salvage pathways"
```

## Literature Search Strategies

### Effective Search Queries

**For pathway context:**
```
"[metabolite] metabolism pathway"
"[metabolite] biosynthesis regulation"
```

**For mechanistic insights:**
```
"[condition] [metabolite] mechanism"
"[enzyme] regulation [condition]"
```

**For flux studies:**
```
"[pathway] flux analysis"
"[metabolite] turnover rate"
```

### Key Databases

1. **KEGG**: Pathway maps and enzyme info
2. **HMDB**: Human Metabolome Database
3. **PubChem**: Chemical structures and properties
4. **MetaboAnalyst**: Analysis tools and pathway info

## Common Pitfalls

❌ **Assuming directionality**
- Many reactions are reversible
- Check enzyme and equilibrium constants

❌ **Ignoring compartmentalization**
- Metabolites exist in different cellular compartments
- Mitochondrial vs cytoplasmic pools may differ

❌ **Overinterpreting single metabolites**
- Always consider pathway context
- One metabolite change can have multiple explanations

❌ **Confusing correlation with regulation**
- Co-regulation doesn't mean direct interaction
- Use pathway knowledge to infer relationships

❌ **Forgetting isomers**
- Many metabolites have isomers (e.g., leucine/isoleucine)
- Mass spec may not distinguish them

## Quality Checks

Before interpreting results, verify:
- [ ] Metabolite identifications are confident (not just m/z matches)
- [ ] Normalization was appropriate (sample weight, protein, etc.)
- [ ] Missing values handled correctly
- [ ] Batch effects addressed
- [ ] Biological replicates have reasonable variance

## Example Analysis Flow

**Observation:** ATP levels decreased 30% (p=0.01)

**Step 1: Check related metabolites**
```python
# Check adenylate pool
print(data[["ATP", "ADP", "AMP"]])
```

**Step 2: Calculate energy charge**
```python
AEC = (ATP + 0.5*ADP) / (ATP + ADP + AMP)
```

**Step 3: Search literature**
```
search_pubmed("[condition] ATP depletion mechanism")
```

**Step 4: Generate hypotheses**
- H1: Increased energy demand (check ATP consumers)
- H2: Reduced ATP synthesis (check TCA metabolites)
- H3: ATP degradation (check breakdown products)

**Step 5: Test hypotheses**
```python
# H2: Check TCA cycle metabolites
tca_metabolites = ["Citrate", "Isocitrate", "α-KG", "Succinate", "Fumarate", "Malate"]
test_pathway(tca_metabolites, group_var)
```

## Key Principle

**Metabolism is a network, not a list.**

Single metabolite changes are clues, not answers. Build mechanistic models by connecting metabolites through known biochemical pathways.
