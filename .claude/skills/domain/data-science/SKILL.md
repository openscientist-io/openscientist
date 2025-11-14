---
name: data-science
description: Statistical analysis strategies and data exploration techniques
---

# Data Science for Scientific Discovery

## When to Use This Skill

- When choosing appropriate statistical tests
- When exploring data structure
- When validating assumptions
- When interpreting statistical results

## Data Exploration

### Initial Data Assessment

**Always start with:**

```python
import pandas as pd
import numpy as np

# Basic info
print(f"Shape: {data.shape}")
print(f"Columns: {data.columns.tolist()}")
print(f"Data types:\n{data.dtypes}")

# Missing values
print(f"Missing values:\n{data.isnull().sum()}")

# Summary statistics
print(data.describe())

# Check for duplicates
print(f"Duplicate rows: {data.duplicated().sum()}")
```

### Distribution Checks

**Before running statistical tests, check distributions:**

```python
import matplotlib.pyplot as plt
import seaborn as sns
from scipy import stats

# Visual check
fig, axes = plt.subplots(2, 2, figsize=(10, 8))

# Histogram
axes[0, 0].hist(data["variable"], bins=30)
axes[0, 0].set_title("Histogram")

# Q-Q plot for normality
stats.probplot(data["variable"], dist="norm", plot=axes[0, 1])
axes[0, 1].set_title("Q-Q Plot")

# Box plot (check for outliers)
axes[1, 0].boxplot(data["variable"])
axes[1, 0].set_title("Box Plot")

# Violin plot by group
if "group" in data.columns:
    sns.violinplot(data=data, x="group", y="variable", ax=axes[1, 1])
    axes[1, 1].set_title("Distribution by Group")

plt.tight_layout()
plt.savefig("distribution_check.png")
```

**Statistical normality tests:**
```python
# Shapiro-Wilk test (n < 50)
stat, p = stats.shapiro(data["variable"])
print(f"Shapiro-Wilk: p={p:.4f}")

# Kolmogorov-Smirnov test (n >= 50)
stat, p = stats.kstest(data["variable"], 'norm')
print(f"K-S test: p={p:.4f}")

# Interpretation: p < 0.05 → reject normality
```

## Choosing Statistical Tests

### Decision Tree

```
What type of comparison?
│
├─> Two groups, continuous outcome
│   ├─> Normally distributed → Independent t-test
│   ├─> Non-normal → Mann-Whitney U test
│   └─> Paired samples → Paired t-test or Wilcoxon
│
├─> Multiple groups (3+), continuous outcome
│   ├─> Normally distributed → One-way ANOVA
│   ├─> Non-normal → Kruskal-Wallis test
│   └─> Multiple factors → Two-way ANOVA or mixed models
│
├─> Categorical outcome
│   ├─> 2x2 table → Chi-square or Fisher's exact
│   └─> Larger table → Chi-square test
│
└─> Association between continuous variables
    ├─> Linear relationship → Pearson correlation
    ├─> Monotonic relationship → Spearman correlation
    └─> Prediction → Linear regression
```

### Implementation Examples

**T-test (two groups, normal data):**
```python
from scipy.stats import ttest_ind

group1 = data[data["group"] == "A"]["variable"]
group2 = data[data["group"] == "B"]["variable"]

t_stat, p_value = ttest_ind(group1, group2)
print(f"t-test: t={t_stat:.3f}, p={p_value:.4f}")

# Effect size (Cohen's d)
mean_diff = group1.mean() - group2.mean()
pooled_std = np.sqrt((group1.std()**2 + group2.std()**2) / 2)
cohens_d = mean_diff / pooled_std
print(f"Cohen's d: {cohens_d:.3f}")
```

**ANOVA (multiple groups, normal data):**
```python
from scipy.stats import f_oneway

groups = [data[data["group"] == g]["variable"] for g in data["group"].unique()]
f_stat, p_value = f_oneway(*groups)
print(f"ANOVA: F={f_stat:.3f}, p={p_value:.4f}")

# Effect size (eta-squared)
# If significant, do post-hoc tests
if p_value < 0.05:
    from scipy.stats import tukey_hsd
    res = tukey_hsd(*groups)
    print(res)
```

**Mann-Whitney U (two groups, non-normal):**
```python
from scipy.stats import mannwhitneyu

u_stat, p_value = mannwhitneyu(group1, group2, alternative='two-sided')
print(f"Mann-Whitney U: U={u_stat:.3f}, p={p_value:.4f}")
```

**Kruskal-Wallis (multiple groups, non-normal):**
```python
from scipy.stats import kruskal

h_stat, p_value = kruskal(*groups)
print(f"Kruskal-Wallis: H={h_stat:.3f}, p={p_value:.4f}")
```

**Correlation:**
```python
from scipy.stats import pearsonr, spearmanr

# Pearson (linear relationship)
r, p = pearsonr(data["var1"], data["var2"])
print(f"Pearson r={r:.3f}, p={p:.4f}")

# Spearman (monotonic relationship)
rho, p = spearmanr(data["var1"], data["var2"])
print(f"Spearman ρ={rho:.3f}, p={p:.4f}")
```

## Multiple Testing Correction

**When:** Testing multiple hypotheses (e.g., 100 metabolites)

**Problem:** 5% false positive rate × 100 tests = 5 false positives expected

**Solutions:**

```python
from statsmodels.stats.multitest import multipletests

# Run many tests
p_values = [test_function(var) for var in variables]

# Bonferroni correction (conservative)
rejected, p_corrected, _, _ = multipletests(p_values, method='bonferroni')

# False Discovery Rate (less conservative, recommended)
rejected, p_corrected, _, _ = multipletests(p_values, method='fdr_bh')

print(f"Significant after FDR correction: {sum(rejected)}")
```

**When to use:**
- Bonferroni: Small number of tests (<20), need strict control
- FDR (Benjamini-Hochberg): Large number of tests (>20), exploratory analysis

## Dimensionality Reduction

### Principal Component Analysis (PCA)

**When:** High-dimensional data, want to visualize structure

```python
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler

# Standardize features (important!)
scaler = StandardScaler()
X_scaled = scaler.fit_transform(data[feature_columns])

# Fit PCA
pca = PCA(n_components=2)
pc_scores = pca.fit_transform(X_scaled)

# Variance explained
print(f"Variance explained: {pca.explained_variance_ratio_}")

# Plot
plt.figure(figsize=(8, 6))
for group in data["group"].unique():
    mask = data["group"] == group
    plt.scatter(pc_scores[mask, 0], pc_scores[mask, 1], label=group, alpha=0.6)
plt.xlabel(f"PC1 ({pca.explained_variance_ratio_[0]:.1%})")
plt.ylabel(f"PC2 ({pca.explained_variance_ratio_[1]:.1%})")
plt.legend()
plt.title("PCA")
plt.savefig("pca.png")
```

**Interpretation:**
- Separation by group → systematic differences
- Outliers → potential batch effects or errors
- PC loadings → which features drive separation

### t-SNE (for visualization only)

**When:** PCA doesn't show clear separation

```python
from sklearn.manifold import TSNE

tsne = TSNE(n_components=2, random_state=42, perplexity=30)
tsne_scores = tsne.fit_transform(X_scaled)

# Plot similar to PCA
```

**Warning:** t-SNE is non-linear and stochastic. Don't overinterpret distances.

## Clustering

### K-means Clustering

**When:** Grouping samples by similarity (unsupervised)

```python
from sklearn.cluster import KMeans

# Determine optimal k using elbow method
inertias = []
for k in range(2, 11):
    kmeans = KMeans(n_clusters=k, random_state=42)
    kmeans.fit(X_scaled)
    inertias.append(kmeans.inertia_)

plt.plot(range(2, 11), inertias, marker='o')
plt.xlabel("Number of clusters")
plt.ylabel("Inertia")
plt.title("Elbow Method")
plt.savefig("elbow_plot.png")

# Fit with chosen k
kmeans = KMeans(n_clusters=3, random_state=42)
clusters = kmeans.fit_predict(X_scaled)
data["cluster"] = clusters
```

### Hierarchical Clustering

**When:** Want dendrogram or nested clusters

```python
from scipy.cluster.hierarchy import dendrogram, linkage
from scipy.spatial.distance import pdist

# Compute linkage
Z = linkage(X_scaled, method='ward')

# Plot dendrogram
plt.figure(figsize=(10, 6))
dendrogram(Z, labels=data["sample_id"].values, leaf_font_size=8)
plt.title("Hierarchical Clustering")
plt.xlabel("Sample")
plt.ylabel("Distance")
plt.savefig("dendrogram.png")
```

## Feature Selection

### Univariate Feature Selection

**When:** Reduce features for modeling

```python
from sklearn.feature_selection import SelectKBest, f_classif

# Select top k features
selector = SelectKBest(score_func=f_classif, k=10)
X_selected = selector.fit_transform(X, y)

# Get feature names
feature_scores = pd.DataFrame({
    "feature": feature_columns,
    "score": selector.scores_,
    "p_value": selector.pvalues_
}).sort_values("score", ascending=False)

print(feature_scores.head(10))
```

### Recursive Feature Elimination

**When:** Want model-based feature selection

```python
from sklearn.feature_selection import RFE
from sklearn.linear_model import LogisticRegression

model = LogisticRegression()
rfe = RFE(estimator=model, n_features_to_select=10)
rfe.fit(X_scaled, y)

selected_features = [f for f, selected in zip(feature_columns, rfe.support_) if selected]
print(f"Selected features: {selected_features}")
```

## Power Analysis

**When:** Planning analysis or interpreting negative results

```python
from statsmodels.stats.power import ttest_power

# For t-test
effect_size = 0.5  # Cohen's d
alpha = 0.05
n_per_group = 20

power = ttest_power(effect_size, n_per_group, alpha)
print(f"Power: {power:.3f}")

# If power < 0.8, you might miss true effects
```

## Confounders and Covariates

### Check for Confounders

**Always check:**
- Age, sex, batch, technical factors

```python
# Check if confounder is associated with both exposure and outcome
from scipy.stats import chi2_contingency

# Confounder vs exposure
table1 = pd.crosstab(data["sex"], data["group"])
chi2, p1, _, _ = chi2_contingency(table1)
print(f"Sex vs Group: p={p1:.4f}")

# Confounder vs outcome
t, p2 = ttest_ind(data[data["sex"]=="M"]["outcome"],
                  data[data["sex"]=="F"]["outcome"])
print(f"Sex vs Outcome: p={p2:.4f}")

# If both p < 0.05, sex is a confounder
```

### Adjust for Covariates

```python
from statsmodels.formula.api import ols
from statsmodels.stats.anova import anova_lm

# ANCOVA (adjusting for continuous covariate)
model = ols('outcome ~ C(group) + age + sex', data=data).fit()
print(anova_lm(model))
```

## Common Mistakes

❌ **Not checking assumptions**
- Always verify normality, homogeneity of variance
- Use diagnostic plots

❌ **P-hacking**
- Don't try multiple tests until one is significant
- Pre-specify analysis plan

❌ **Ignoring multiple testing**
- Correct p-values when testing many hypotheses
- Use FDR for large-scale screens

❌ **Overinterpreting p-values**
- p=0.051 is not "failed", p=0.049 is not "proof"
- Report effect sizes and confidence intervals

❌ **Using wrong test**
- Non-normal data with t-test → use non-parametric
- Paired data with independent test → use paired test

❌ **Correlation = causation**
- Always consider confounders
- Use mechanistic reasoning

## Reporting Statistical Results

**Good reporting includes:**

1. **Test used**: "Two-sample t-test"
2. **Test statistic**: "t = 2.45"
3. **P-value**: "p = 0.028"
4. **Effect size**: "Cohen's d = 0.82 (large effect)"
5. **Sample size**: "n₁ = 15, n₂ = 15"
6. **Confidence interval**: "95% CI: [0.5, 3.2]"

**Example:**
```
Group A (n=15, mean±SD: 12.3±2.1) had significantly higher levels
than Group B (n=15, 9.1±1.8; t(28)=2.45, p=0.028, d=0.82, 95% CI: [0.5, 3.2]).
```

## Key Principle

**Statistics are tools for inference, not magic.**

Understand what each test assumes and when it's appropriate. Always combine statistical significance with domain knowledge and effect sizes.
