---
name: genomics
description: Genomics and transcriptomics analysis strategies
category: domain
---

# Genomics and Transcriptomics Analysis

## When to Use This Skill

- When data contains gene expression measurements (RNA-seq, microarray)
- When analyzing differential gene expression
- When performing pathway or gene set enrichment analysis
- When interpreting genetic variants or mutations

## Core Concepts

### Gene Expression Data Types

**RNA-seq counts:**
- Raw read counts per gene
- Requires normalization (TPM, RPKM, DESeq2)
- Suitable for differential expression analysis

**Microarray intensities:**
- Probe fluorescence intensities
- Log-transformed, background-corrected
- Legacy platform, less common now

**Single-cell RNA-seq:**
- Expression per cell (not bulk tissue)
- High sparsity (many zeros)
- Specialized analysis methods
- ⚠️ For differential expression *across conditions*, aggregate to the sample level
  (pseudobulk) — cells are **not** independent replicates (see below)

### Gene Nomenclature

**Human genes:**
- Official symbols: HUGO Gene Nomenclature Committee (HGNC)
- Example: *TP53* (tumor protein p53)
- Italicized in publications

**Mouse genes:**
- Similar to human but capitalization differs
- Example: *Tp53* (first letter capital, rest lowercase)

**Protein names:**
- Not italicized
- Example: p53 protein

**Always verify gene symbols** - aliases and outdated names are common.

## Differential Expression Analysis

### Workflow

```python
import pandas as pd
import numpy as np
from scipy.stats import ttest_ind
from statsmodels.stats.multitest import multipletests

# Load expression data (genes × samples)
# Rows = genes, Columns = samples
expr_data = pd.read_csv("expression_data.csv", index_col=0)

# Define groups
group1_samples = ["Sample1", "Sample2", "Sample3"]
group2_samples = ["Sample4", "Sample5", "Sample6"]

results = []

for gene in expr_data.index:
    group1_expr = expr_data.loc[gene, group1_samples]
    group2_expr = expr_data.loc[gene, group2_samples]

    # T-test
    t_stat, p_value = ttest_ind(group1_expr, group2_expr)

    # Fold change
    mean1 = group1_expr.mean()
    mean2 = group2_expr.mean()
    log2fc = np.log2(mean1 / mean2) if mean2 > 0 else np.nan

    results.append({
        "gene": gene,
        "log2FC": log2fc,
        "p_value": p_value,
        "mean_group1": mean1,
        "mean_group2": mean2
    })

results_df = pd.DataFrame(results)

# Multiple testing correction
results_df["p_adj"] = multipletests(results_df["p_value"], method="fdr_bh")[1]

# Define significant genes
significant = results_df[
    (results_df["p_adj"] < 0.05) &
    (abs(results_df["log2FC"]) > 1)  # 2-fold change
]

print(f"Significant genes: {len(significant)}")
print(f"Upregulated: {sum(significant['log2FC'] > 0)}")
print(f"Downregulated: {sum(significant['log2FC'] < 0)}")
```

### Volcano Plot

**Visualize differential expression:**

```python
import matplotlib.pyplot as plt

plt.figure(figsize=(8, 6))
plt.scatter(
    results_df["log2FC"],
    -np.log10(results_df["p_adj"]),
    alpha=0.5, s=10, c="gray"
)

# Highlight significant genes
sig_mask = (results_df["p_adj"] < 0.05) & (abs(results_df["log2FC"]) > 1)
plt.scatter(
    results_df.loc[sig_mask, "log2FC"],
    -np.log10(results_df.loc[sig_mask, "p_adj"]),
    alpha=0.7, s=20, c="red", label="Significant"
)

plt.xlabel("log2 Fold Change")
plt.ylabel("-log10(adjusted p-value)")
plt.axhline(-np.log10(0.05), linestyle="--", color="black", linewidth=0.5)
plt.axvline(-1, linestyle="--", color="black", linewidth=0.5)
plt.axvline(1, linestyle="--", color="black", linewidth=0.5)
plt.title("Volcano Plot")
plt.legend()
plt.savefig("volcano_plot.png", dpi=300)
```

## Single-Cell Differential Expression: Beware Pseudoreplication

⚠️ **The most common and most consequential error in single-cell DE.** A typical
single-cell experiment has *many cells* (tens of thousands) but *few biological
samples* (a handful of patients or animals per condition). Cells from the same
donor are **not independent** — they share genetic background, batch, dissociation
and technical effects. The unit of replication is the **sample/donor, not the cell**.

Running a per-cell test (t-test, Wilcoxon, naive `rank_genes_groups`) and treating
each cell as a replicate silently inflates the effective n from ~6 samples to
~60,000 cells. This collapses the variance estimate and produces enormous
false-positive rates — naive cell-level tests call hundreds of "significant" genes
even when comparing two random splits of the *same* population, with highly
expressed genes flagged most often (Squair et al. 2021; Zimmerman et al. 2021).

**Required workflow: run both analyses below, then compare them.** For any
condition-level single-cell DE, do not pick one method. Run **(1)** a pseudobulk
analysis as the first pass and **(2)** a donor-random-intercept mixed-effects model,
then reconcile the two gene lists (see "Compare the two analyses"). Agreement
between an aggregation-based and a cell-level method that both respect the
sample/donor as the replicate unit is your best guard against method-specific
artifacts.

### Analysis 1 — Pseudobulk (first pass)

**When:** Comparing a given cell type across conditions in a multi-sample design.
This is the field-standard default and the top performer for FDR control in
benchmarks (Squair et al. 2021; Murphy & Skene 2022; Junttila et al. 2022).

Sum **raw counts** across all cells of one cell type within each sample, then run a
bulk DE tool on the resulting sample-level matrix. **Prefer DESeq2** (or
edgeR/limma-voom) over a paired t-test on the aggregated profiles: it models the
count mean–variance relationship and shares information across genes, whereas a
per-gene paired t-test is underpowered at typical sample sizes and assumes
normality the counts don't satisfy. The statistics then reduce to the bulk
RNA-seq workflow above, with an honest n.

```python
import anndata as ad
import pandas as pd

# adata: cells × genes; obs has "sample_id", "cell_type", "condition"
adata = ad.read_h5ad("single_cell.h5ad")

# Aggregate raw counts -> one pseudobulk profile per sample, for one cell type
def pseudobulk(adata, cell_type):
    sub = adata[adata.obs["cell_type"] == cell_type]
    X = sub.X.toarray() if hasattr(sub.X, "toarray") else sub.X
    counts = pd.DataFrame(X, index=sub.obs["sample_id"].values, columns=sub.var_names)
    # SUM counts within each sample (not mean — DESeq2 expects counts)
    return counts.groupby(level=0).sum()  # samples × genes

pb = pseudobulk(adata, cell_type="Microglia")

# One metadata row per sample, aligned to the pseudobulk matrix
sample_meta = (
    adata.obs[["sample_id", "condition"]]
    .drop_duplicates()
    .set_index("sample_id")
    .loc[pb.index]
)

# DESeq2 on the sample-level matrix (n = number of samples, not cells)
from pydeseq2.dds import DeseqDataSet
from pydeseq2.ds import DeseqStats

dds = DeseqDataSet(counts=pb, metadata=sample_meta, design_factors="condition")
dds.deseq2()
res = DeseqStats(dds, contrast=["condition", "disease", "control"])
res.summary()
print(res.results_df.sort_values("padj").head())
```

`muscat` (R/Bioconductor; Crowell et al. 2020) implements this aggregation-based
workflow across all cell types at once if you prefer a turnkey tool.

### Analysis 2 — Mixed-effects model (random intercept per donor)

**When:** Always, as the second pass alongside pseudobulk. The mixed model retains
cell-level resolution and handles continuous/complex designs. A (generalized)
linear mixed model with a **random intercept per donor** models the within-donor
correlation directly, so the donor — not the cell — remains the unit of
replication (Zimmerman et al. 2021).

```python
import statsmodels.formula.api as smf

# Long dataframe: one row per cell, columns "expression", "condition", "sample_id"
# The random intercept per donor absorbs donor-level correlation.
model = smf.mixedlm("expression ~ condition", data=cell_df, groups=cell_df["sample_id"])
result = model.fit()
print(result.summary())  # the fixed effect of `condition` is the test of interest
```

**Tradeoffs:** heavier compute, convergence/fitting fragility, and sensitivity to
the assumed count distribution. In benchmarks weighing both sensitivity *and*
specificity, pseudobulk generally matches or beats mixed models (Murphy & Skene
2022) — which is why pseudobulk is the first pass and its call list is the primary
reference when the two disagree.

### Compare the two analyses

Reconcile the pseudobulk and mixed-model results before drawing conclusions:

- **Concordant genes** (significant in both, same direction) are the confident
  hits — report these as the core finding.
- **Pseudobulk-only** hits are usually trustworthy given its FDR control; keep them
  but note they lack cell-level confirmation.
- **Mixed-model-only** hits warrant scrutiny — check for convergence warnings,
  a poorly fit count distribution, or a handful of dominant donors driving the
  effect before believing them.
- **Large disagreement** overall signals a problem: too few donors, an unmodeled
  batch/covariate, or a distributional assumption violated. Investigate rather
  than cherry-picking the list you prefer.

Quantify overlap (e.g. shared significant genes, rank correlation of effect sizes
or a scatter of the two log-fold-change estimates) and report it alongside the
gene lists.

### Replication

Power for condition-level DE scales with the **number of independent samples**,
not the number of cells per sample. Aim for **≥3 samples per condition** (more is
better); adding cells per donor does not buy replication (Zimmerman et al. 2021;
Heumos et al. 2023).

## Gene Set Enrichment

### Simple Pathway Enrichment

**When:** You have a list of significant genes and want to know which pathways are affected

```python
# Define gene sets (pathways)
gene_sets = {
    "Cell Cycle": ["TP53", "CDK1", "CCNB1", "CDC20", ...],
    "Apoptosis": ["TP53", "BAX", "BCL2", "CASP3", ...],
    "DNA Repair": ["TP53", "BRCA1", "BRCA2", "ATM", ...],
    # ... more pathways
}

# Fisher's exact test for enrichment
from scipy.stats import fisher_exact

all_genes = set(expr_data.index)
sig_genes = set(significant["gene"])

enrichment_results = []

for pathway, pathway_genes in gene_sets.items():
    pathway_genes = set(pathway_genes) & all_genes  # Only genes in dataset

    # 2x2 contingency table
    a = len(sig_genes & pathway_genes)  # Sig & in pathway
    b = len(sig_genes - pathway_genes)  # Sig & not in pathway
    c = len(pathway_genes - sig_genes)  # Not sig & in pathway
    d = len(all_genes - sig_genes - pathway_genes)  # Not sig & not in pathway

    oddsratio, p_value = fisher_exact([[a, b], [c, d]], alternative='greater')

    enrichment_results.append({
        "pathway": pathway,
        "overlap": a,
        "pathway_size": len(pathway_genes),
        "odds_ratio": oddsratio,
        "p_value": p_value
    })

enrich_df = pd.DataFrame(enrichment_results)
enrich_df["p_adj"] = multipletests(enrich_df["p_value"], method="fdr_bh")[1]
enrich_df = enrich_df.sort_values("p_adj")

print(enrich_df.head(10))
```

### Gene Ontology (GO) Terms

**Common GO categories:**
- **Biological Process (BP)**: What the gene does (e.g., "cell cycle", "apoptosis")
- **Molecular Function (MF)**: Biochemical activity (e.g., "kinase activity")
- **Cellular Component (CC)**: Where it acts (e.g., "nucleus", "mitochondrion")

**Resources:**
- Gene Ontology: http://geneontology.org/
- Enrichr: https://maayanlab.cloud/Enrichr/ (web-based enrichment)
- DAVID: https://david.ncifcrf.gov/

### KEGG Pathway Enrichment

**KEGG = Kyoto Encyclopedia of Genes and Genomes**

Provides curated pathway maps for:
- Metabolic pathways
- Signaling pathways
- Disease pathways

**Example pathways:**
- hsa04110: Cell cycle
- hsa04210: Apoptosis
- hsa04151: PI3K-Akt signaling

## Common Analysis Patterns

### Pattern 1: Transcription Factor Activity

**Observation:** Many genes upregulated

**Hypothesis:** Shared transcription factor (TF)

**Test:**
```python
# Check if significant genes share TF binding motifs
tf_targets = {
    "TP53": ["BAX", "CDKN1A", "MDM2", "GADD45A", ...],
    "MYC": ["CDK4", "CCND1", "E2F1", ...],
    # ... more TFs
}

# Test for enrichment (same as pathway enrichment)
```

**Interpretation:** Enrichment suggests TF is active/inactive in condition

### Pattern 2: Pathway Coordination

**Observation:** Genes in same pathway all up/down together

**Interpretation:** Pathway-level regulation (not individual genes)

**Example:**
```
All glycolysis genes ↑↑ → Increased glycolysis
All oxidative phosphorylation genes ↓↓ → Metabolic shift
```

### Pattern 3: Compensatory Response

**Observation:** Opposite regulation of related pathways

**Example:**
```
De novo biosynthesis genes ↓
Salvage pathway genes ↑
→ Metabolic switch to energy-efficient salvage
```

## Correlation Analysis

### Co-expression Networks

**When:** Identify genes that change together

```python
from scipy.stats import pearsonr

# Compute pairwise correlations
genes = significant["gene"].tolist()[:50]  # Top 50 for tractability
corr_matrix = expr_data.loc[genes].T.corr()

# Filter high correlations
high_corr = []
for i in range(len(genes)):
    for j in range(i+1, len(genes)):
        if abs(corr_matrix.iloc[i, j]) > 0.8:
            high_corr.append({
                "gene1": genes[i],
                "gene2": genes[j],
                "correlation": corr_matrix.iloc[i, j]
            })

print(f"High correlations (|r| > 0.8): {len(high_corr)}")
```

**Interpretation:**
- Positive correlation → co-regulated (same pathway, shared TF)
- Negative correlation → antagonistic regulation

### Network Visualization

```python
import networkx as nx

# Build network
G = nx.Graph()
for item in high_corr:
    G.add_edge(item["gene1"], item["gene2"], weight=abs(item["correlation"]))

# Find communities (clusters of co-expressed genes)
from networkx.algorithms import community
communities = community.greedy_modularity_communities(G)

for i, comm in enumerate(communities):
    print(f"Community {i}: {list(comm)}")
```

## Literature Search Strategies

### Effective Queries

**For gene function:**
```
"[GENE] function"
"[GENE] role in [PROCESS]"
"[GENE] knockout phenotype"
```

**For pathway context:**
```
"[GENE] pathway"
"[GENE] interacting proteins"
"[GENE] regulation"
```

**For disease relevance:**
```
"[GENE] [DISEASE]"
"[GENE] mutation [DISEASE]"
```

### Key Databases

1. **NCBI Gene**: Gene summaries and references
2. **UniProt**: Protein function and domains
3. **STRING**: Protein-protein interactions
4. **GeneCards**: Comprehensive gene info
5. **PubMed**: Literature search

## Genomics-Specific Hypotheses

### Template Hypotheses

**H1: Transcriptional Regulation**
```
"Condition X activates transcription factor [TF], upregulating
target genes [G1, G2, G3] in pathway [P]"
```

**H2: Pathway Activation**
```
"Condition X activates [pathway], evidenced by coordinated
upregulation of pathway genes and increased activity signature"
```

**H3: Epigenetic Regulation**
```
"Condition X alters chromatin state at [locus], changing
expression of genes [G1, G2]"
```

**H4: Post-transcriptional Regulation**
```
"MicroRNA [miR] is upregulated, suppressing target genes [G1, G2],
explaining decreased protein levels despite unchanged mRNA"
```

## Quality Control

Before interpreting results:

- [ ] Check for batch effects (PCA colored by batch)
- [ ] Verify sample labels are correct
- [ ] Check for outlier samples (hierarchical clustering)
- [ ] Confirm expression distribution (should be roughly normal after log transform)
- [ ] Verify normalization (samples should have similar distributions)

## Common Pitfalls

❌ **Ignoring log transformation**
- Expression data should be log-transformed for most analyses
- Fold changes are linear differences in log space

❌ **Using nominal p-values for many genes**
- Always correct for multiple testing (FDR)
- Use adjusted p-values for significance

❌ **Overinterpreting small fold changes**
- log2FC < 0.5 (1.4-fold) may not be biologically meaningful
- Use stricter thresholds for noisy data

❌ **Confusing gene expression with protein activity**
- mRNA ≠ protein levels
- Protein activity may require post-translational modifications

❌ **Cherry-picking genes**
- Don't select genes to fit a story
- Use unbiased pathway enrichment

❌ **Treating single cells as biological replicates (pseudoreplication)**
- Cells from one donor are not independent; the replicate unit is the sample/donor
- A naive per-cell t-test/Wilcoxon inflates n by orders of magnitude → false positives
- Run both a pseudobulk analysis and a donor-random-intercept mixed model, then
  compare — see "Single-Cell Differential Expression: Beware Pseudoreplication" above

## Integration with Other Data Types

### Transcriptomics + Metabolomics

**Strategy:**
```
1. Identify differentially expressed metabolic enzymes
2. Map to KEGG pathways
3. Check if corresponding metabolites are changed
4. Build integrated metabolic model
```

**Example:**
```
Gene: PHGDH (phosphoglycerate dehydrogenase) ↑
Metabolite: Serine ↑
→ Integrated finding: Increased serine biosynthesis
```

### Transcriptomics + Proteomics

**Compare mRNA vs protein changes:**
- Concordant (both up/down) → transcriptional regulation
- Discordant (mRNA ≠ protein) → post-transcriptional regulation

## Key References

**Single-cell pseudoreplication and differential expression:**

1. Squair et al. (2021) *Confronting false discoveries in single-cell differential expression.* Nat Commun. https://doi.org/10.1038/s41467-021-25960-2
2. Zimmerman et al. (2021) *A practical solution to pseudoreplication bias in single-cell studies.* Nat Commun. https://doi.org/10.1038/s41467-021-21038-1
3. Crowell et al. (2020) *muscat detects subpopulation-specific state transitions from multi-sample multi-condition single-cell transcriptomics data.* Nat Commun. https://doi.org/10.1038/s41467-020-19894-4
4. Murphy & Skene (2022) *A balanced measure shows superior performance of pseudobulk methods in single-cell RNA-sequencing analysis.* Nat Commun. https://doi.org/10.1038/s41467-022-35519-4
5. Junttila et al. (2022) *Benchmarking methods for detecting differential states between conditions from multi-subject single-cell RNA-seq data.* Brief Bioinform. https://doi.org/10.1093/bib/bbac286
6. Hurlbert (1984) *Pseudoreplication and the design of ecological field experiments.* Ecol Monogr. https://doi.org/10.2307/1942661
7. Heumos et al. (2023) *Best practices for single-cell analysis across modalities.* Nat Rev Genet. https://doi.org/10.1038/s41576-023-00586-w — practical DE walkthrough: https://www.sc-best-practices.org/conditions/differential_gene_expression.html

## Key Principle

**Gene expression is the messenger, not the message.**

mRNA changes indicate *potential* for protein changes. Always consider:
- Post-transcriptional regulation (miRNA, RNA stability)
- Translational control
- Protein stability and degradation
- Post-translational modifications

Connect expression changes to phenotype through pathways and functional validation.
