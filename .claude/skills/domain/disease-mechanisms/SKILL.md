---
name: disease-mechanisms
description: Validate and discover disease mechanisms using Perturb-seq and genetic association data
---

# Disease Mechanisms Analysis

## When to Use This Skill

- When validating curated disease mechanisms against genetic data
- When analyzing gene-phenotype relationships for a specific disorder
- When discovering new regulatory pathways from Perturb-seq data
- When integrating loss-of-function burden tests with gene expression data

## Background: The Ota Framework

This skill implements the approach from Ota et al. (Nature 2025) for building causal
gene→program→trait graphs:

### Key Concepts

| Term | Definition |
|------|------------|
| **LoF burden (γ)** | Effect of gene loss-of-function on trait (from UK Biobank) |
| **GeneBayes** | Empirical Bayes framework for improved γ estimation |
| **Perturb-seq (β)** | Regulatory effect of gene knockdown on target expression |
| **Program** | Co-regulated gene module from cNMF decomposition |
| **Program burden** | Average γ of program genes → measures program-trait causality |
| **Regulator-burden correlation** | Correlation of β with γ across genes → validates regulatory paths |

### Interpretation Guide

**LoF burden effect (γ):**
- Positive γ: Loss-of-function **increases** trait value
- Negative γ: Loss-of-function **decreases** trait value
- |γ| > 0.1 generally considered significant

**Perturb-seq effect (β):**
- Positive β: Knockdown **increases** target expression
- Negative β: Knockdown **decreases** target expression
- Represents causal regulatory relationship

**Program burden effect:**
- Positive: Program genes' LoF increases trait → program **represses** trait
- Negative: Program genes' LoF decreases trait → program **promotes** trait

## Available MCP Tools

### query_lof_burden(gene, trait)

Query GeneBayes posterior effect size for a gene's LoF on a trait.

**Parameters:**
- `gene`: HGNC symbol (e.g., "HBB", "KLF1")
- `trait`: UKB trait code (e.g., "MCH", "RDW", "IRF")

**Returns:**
```python
{
    "gene": "HBB",
    "trait": "MCH",
    "effect_size": -1.5,      # γ posterior mean
    "se": 0.08,               # standard error
    "p_value": 1e-50,
    "source": "UKB_LoF_GeneBayes"
}
```

**Example:**
```python
# Check if HBB loss-of-function affects MCH
result = query_lof_burden("HBB", "MCH")
# Expect: large negative effect (LoF decreases hemoglobin)
```

### query_perturbseq(gene, target, cell_type="K562")

Query Perturb-seq regulatory effect of gene knockdown on target.

**Parameters:**
- `gene`: Gene that was knocked down
- `target`: Gene whose expression was measured
- `cell_type`: Cell line ("K562", "HepG2", "Jurkat")

**Returns:**
```python
{
    "perturbed_gene": "KLF1",
    "target_gene": "HBA1",
    "log2fc": -0.8,           # knockdown decreases HBA1
    "p_value": 1e-20,
    "cell_type": "K562",
    "source": "Replogle2022_Perturbseq"
}
```

**Example:**
```python
# Check if KLF1 regulates HBA1
result = query_perturbseq("KLF1", "HBA1")
# Expect: negative log2fc (KLF1 activates HBA1)
```

### query_program_burden(program_id, trait)

Query program-level burden effect on trait.

**Parameters:**
- `program_id`: cNMF program identifier (e.g., "P40_haemoglobin_synthesis")
- `trait`: UKB trait code

**Returns:**
```python
{
    "program": "P40_haemoglobin_synthesis",
    "trait": "MCH",
    "program_burden_effect": -2.3,    # program promotes MCH
    "regulator_burden_correlation": 0.45,
    "p_value": 1e-10,
    "top_genes": ["HBA1", "HBA2", "HBB", "ALAS2", ...]
}
```

### query_dismech(disorder)

Load curated pathophysiology from dismech knowledge base.

**Parameters:**
- `disorder`: Disorder name (e.g., "Sickle_Cell_Disease")

**Returns:**
```python
{
    "name": "Sickle Cell Disease",
    "disease_term": {"id": "MONDO:0011382", "label": "sickle cell disease"},
    "pathophysiology": [
        {
            "name": "Hemoglobin Polymerization",
            "description": "Deoxygenated HbS polymerizes...",
            "biological_processes": [...],
            "genes": [...],
            "evidence": [...]
        },
        ...
    ],
    "phenotypes": [...]
}
```

### validate_mechanism(disorder, gene, process, phenotype, evidence)

Record a validated or refuted mechanism for human review.

**Parameters:**
- `disorder`: Disorder name
- `gene`: Gene involved
- `process`: Biological process (GO term or description)
- `phenotype`: Phenotype affected (HP term or trait)
- `evidence`: Dict with effect sizes, p-values, PMIDs

**Returns:**
```python
{
    "status": "VALIDATED",  # or REFUTED, WEAK_SUPPORT, NOVEL
    "mechanism": "HBB → hemoglobin synthesis → decreased MCH",
    "evidence_strength": "strong",
    "recommendation": "Curated mechanism confirmed by genetic data"
}
```

## Workflow: Validating Curated Mechanisms

### Step 1: Load the Disorder

```python
dismech_data = query_dismech("Sickle_Cell_Disease")
print(f"Disorder: {dismech_data['name']}")
print(f"Pathophysiology steps: {len(dismech_data['pathophysiology'])}")
```

### Step 2: Extract Testable Claims

For each pathophysiology entry, extract:
- **Genes involved** (from gene descriptors)
- **Biological processes** (GO terms)
- **Expected phenotypes** (HP terms → UKB traits)

```python
claims = []
for path in dismech_data['pathophysiology']:
    for gene in path.get('genes', []):
        for pheno in dismech_data['phenotypes']:
            claims.append({
                'gene': gene['term']['label'],
                'process': path['name'],
                'phenotype': pheno['name'],
                'expected_direction': path.get('modifier', 'ABNORMAL')
            })
```

### Step 3: Query Genetic Evidence

```python
for claim in claims:
    # Map phenotype to UKB trait
    trait = map_phenotype_to_trait(claim['phenotype'])

    # Query LoF burden effect
    lof_result = query_lof_burden(claim['gene'], trait)

    # Check if direction matches expectation
    if lof_result['p_value'] < 0.05:
        if matches_expected_direction(lof_result, claim):
            claim['status'] = 'VALIDATED'
        else:
            claim['status'] = 'DIRECTION_MISMATCH'
    else:
        claim['status'] = 'INSUFFICIENT_EVIDENCE'
```

### Step 4: Validate Regulatory Relationships

```python
# Check if regulatory claims are supported by Perturb-seq
for path in dismech_data['pathophysiology']:
    regulators = path.get('regulators', [])
    targets = path.get('target_genes', [])

    for reg in regulators:
        for target in targets:
            result = query_perturbseq(reg, target)
            if result['p_value'] < 0.05:
                print(f"Regulatory path confirmed: {reg} → {target}")
```

### Step 5: Discover Novel Mechanisms

```python
# Find programs with significant burden effects not in dismech
all_programs = get_all_programs()
curated_processes = extract_go_terms(dismech_data)

for program in all_programs:
    result = query_program_burden(program['id'], trait)

    if result['p_value'] < 0.01:
        if program['go_terms'] not in curated_processes:
            # Novel mechanism found!
            print(f"Novel mechanism: {program['name']} affects {trait}")
            search_pubmed(f"{program['name']} {dismech_data['name']}")
```

## Phenotype-to-Trait Mapping

Map dismech HP terms to UK Biobank traits:

| HP Term | Description | UKB Trait | Cell Type |
|---------|-------------|-----------|-----------|
| HP:0001878 | Hemolytic anemia | MCH, RDW | K562 |
| HP:0001903 | Anemia | Hemoglobin, RBC | K562 |
| HP:0001744 | Splenomegaly | RDW | K562 |
| HP:0001923 | Reticulocytosis | IRF, RetCou | K562 |
| HP:0002910 | Elevated liver enzymes | ALT, AST | HepG2 |
| HP:0001399 | Hepatic failure | ALT, AST, Bilirubin | HepG2 |

## Hypothesis Templates

### H1: Gene-Phenotype Validation

```
"dismech claims [GENE] causes [PHENOTYPE] via [PROCESS].
If true:
  - LoF burden for [GENE] on [TRAIT] should be significant
  - Direction should match expected effect
  - Related program burden should be significant"
```

### H2: Regulatory Pathway Validation

```
"dismech claims [REGULATOR] controls [TARGET] in [PROCESS].
If true:
  - Perturb-seq: knockdown of [REGULATOR] should affect [TARGET]
  - Both genes should affect same trait with consistent directions"
```

### H3: Program Discovery

```
"Program [P] shows significant burden effect on [TRAIT].
This program is not curated in dismech for [DISORDER].
Hypothesis: [P] represents an undocumented mechanism.
Test: Search literature for [P genes] + [DISORDER]."
```

### H4: Cross-Trait Pleiotropy

```
"Gene [G] affects both [TRAIT1] and [TRAIT2].
These traits have genetic correlation [r].
Hypothesis: [G] acts through shared pathway [P].
Test: Check if program [P] has significant effects on both traits."
```

## Quality Control

Before reporting findings:

- [ ] Verify gene symbols are valid HGNC
- [ ] Check that trait is available for queried cell type
- [ ] Use FDR correction for multiple testing
- [ ] Report effect sizes, not just p-values
- [ ] Cross-reference with PubMed literature

## Common Patterns

### Pattern 1: Strong Single-Gene Effect

```
HBB LoF: γ = -1.5 on MCH (P < 1e-50)
→ HBB is a core gene for MCH
→ Directly validates dismech claim
```

### Pattern 2: Program-Mediated Effect

```
KLF1 LoF: γ = -0.8 on MCH
KLF1 knockdown: β = -0.9 on HBA1
Haemoglobin synthesis program: significant for MCH
→ KLF1 affects MCH via haemoglobin program
→ Validates regulatory mechanism
```

### Pattern 3: Discordant Direction

```
dismech: Gene X causes increased phenotype
LoF burden: γ is negative (LoF decreases phenotype)
→ MISMATCH: curated direction may be wrong
→ Review evidence, check for context-dependence
```

### Pattern 4: No Genetic Signal

```
dismech: Gene Y causes phenotype Z
LoF burden: γ = 0.01, P = 0.8
→ Insufficient evidence from genetics
→ May still be true (not all genes have LoF carriers)
→ Check Perturb-seq for regulatory evidence
```

## Cell Type Considerations

Different disorders require different cell types:

| Cell Type | Source | Best For |
|-----------|--------|----------|
| **K562** | Erythroleukemia | Blood disorders, anemias |
| **HepG2** | Hepatocellular carcinoma | Liver diseases, metabolism |
| **Jurkat** | T-cell leukemia | Immune disorders |
| **RPE1** | Retinal pigment epithelium | Retinal diseases |

**Important:** Only validate mechanisms in the appropriate cell type!

## Literature Integration

After finding genetic evidence, search PubMed to:

1. Confirm mechanistic interpretation
2. Find additional context
3. Identify conflicting evidence
4. Locate quotable snippets for dismech evidence

**Effective queries:**
```
"[GENE] [DISORDER] mechanism"
"[GENE] [PROCESS] regulation"
"[PROGRAM genes] [TRAIT] genetic"
```

## Output Format

For each validated/discovered mechanism, record:

```yaml
- mechanism: "HBB → hemoglobin synthesis → decreased MCH"
  status: VALIDATED
  gene: HBB
  process: GO:0006783  # heme biosynthetic process
  phenotype: HP:0001878  # Hemolytic anemia
  trait: MCH
  evidence:
    lof_burden_effect: -1.5
    lof_burden_pvalue: 1e-50
    program_burden_effect: -2.3
    program: P40_haemoglobin_synthesis
    supporting_pmids:
      - PMID:15998894
      - PMID:24277079
  interpretation: >
    Loss-of-function of HBB strongly decreases MCH, consistent with
    the curated mechanism that HBB mutations cause reduced hemoglobin
    per erythrocyte. The haemoglobin synthesis program shows concordant
    effects, confirming this is a core pathway for the trait.
```

## Key Principle

**Genetic associations are causal; curated mechanisms are hypotheses.**

Use LoF burden tests and Perturb-seq to validate or refute curated claims.
Discoveries from genetic data are strong candidates for new mechanisms.
Always ground findings in literature with PubMed evidence.
