---
name: meta-analysis
description: >
  Perform quantitative meta-analysis on effect sizes extracted from a set of
  primary studies. Computes pooled estimates (fixed-effects and random-effects),
  heterogeneity statistics (I², Q-test, τ²), Egger's test for publication bias,
  and generates a forest plot. Requires PICO-framed data (use pico-framing skill
  first). Works with OR, RR, HR, MD, and SMD effect measures.
category: domain
---

# Meta-Analysis

## When to Use This Skill

- After completing PICO framing and literature screening
- When ≥2 studies with compatible effect measures have been extracted
- When asked to "pool effect sizes", "do a meta-analysis", or "generate a forest plot"
- Before writing up a GRADE certainty rating (I² from meta-analysis informs inconsistency)

Do NOT attempt to pool studies with incompatible outcomes or effect measures —
report the studies narratively instead and note why pooling was not appropriate.

## Prerequisites

- PICO frame defined (see `pico-framing` skill)
- At least 2 studies with:
  - Effect size (OR, RR, HR, MD, or SMD)
  - 95% confidence interval OR standard error OR sample sizes + event counts
- Studies measuring the **same outcome** with a **compatible metric**

## Input Data Format

Create a Python list of dicts, one per study:

```python
studies = [
    {
        "study_id": "Smith 2021",
        "pmid": "33012345",
        "effect_size": 0.72,       # point estimate (OR, RR, HR, MD, SMD)
        "ci_lower": 0.55,
        "ci_upper": 0.94,
        "n_treatment": 120,
        "n_control": 118,
        "weight": None,            # leave None; computed automatically
    },
    {
        "study_id": "Jones 2023",
        "pmid": "36789012",
        "effect_size": 0.68,
        "ci_lower": 0.50,
        "ci_upper": 0.91,
        "n_treatment": 95,
        "n_control": 92,
        "weight": None,
    },
]
measure_type = "RR"   # OR | RR | HR | MD | SMD
outcome_label = "Annualised relapse rate"
pico_label = "Azathioprine vs placebo in NMOSD"
```

## Python Implementation

Use the following code block. Copy and run it in the executor.

```python
import math
import json
import numpy as np

# ── helpers ──────────────────────────────────────────────────────────────────

def log_se_from_ci(effect_size, ci_lower, ci_upper, log_scale=True):
    """Compute standard error from 95% CI. log_scale=True for OR/RR/HR."""
    if log_scale:
        se = (math.log(ci_upper) - math.log(ci_lower)) / (2 * 1.96)
    else:
        se = (ci_upper - ci_lower) / (2 * 1.96)
    return se

def pool_fixed_effects(effects, ses):
    """Inverse-variance fixed-effects pooling."""
    weights = [1 / se**2 for se in ses]
    pooled = sum(w * e for w, e in zip(weights, effects)) / sum(weights)
    se_pooled = math.sqrt(1 / sum(weights))
    return pooled, se_pooled, weights

def pool_random_effects_dl(effects, ses):
    """DerSimonian-Laird random-effects pooling."""
    k = len(effects)
    weights_fe = [1 / se**2 for se in ses]
    pooled_fe = sum(w * e for w, e in zip(weights_fe, effects)) / sum(weights_fe)

    # Q statistic
    Q = sum(w * (e - pooled_fe)**2 for w, e in zip(weights_fe, effects))
    df = k - 1

    # τ² (between-study variance)
    C = sum(weights_fe) - sum(w**2 for w in weights_fe) / sum(weights_fe)
    tau2 = max(0, (Q - df) / C)

    # Updated weights
    weights_re = [1 / (se**2 + tau2) for se in ses]
    pooled_re = sum(w * e for w, e in zip(weights_re, effects)) / sum(weights_re)
    se_pooled_re = math.sqrt(1 / sum(weights_re))

    # I²
    i2 = max(0, (Q - df) / Q * 100) if Q > 0 else 0.0

    return pooled_re, se_pooled_re, weights_re, tau2, i2, Q, df

def z_to_p(z):
    """Two-tailed p-value from z-score."""
    from scipy import stats
    return 2 * (1 - stats.norm.cdf(abs(z)))

def egger_test(effects, ses):
    """Egger's regression test for small-study effects / publication bias."""
    from scipy import stats
    precision = [1 / se for se in ses]
    standardised = [e / se for e, se in zip(effects, ses)]
    slope, intercept, r, p, stderr = stats.linregress(precision, standardised)
    return {"intercept": intercept, "intercept_se": stderr, "p_value": p,
            "interpretation": "p<0.05 suggests asymmetry (possible publication bias)" if p < 0.05
                              else "p≥0.05, no significant funnel plot asymmetry detected"}


# ── main analysis ─────────────────────────────────────────────────────────────

def run_meta_analysis(studies, measure_type, outcome_label, pico_label):
    log_scale = measure_type in ("OR", "RR", "HR")

    # Transform to log scale for ratio measures
    if log_scale:
        effects = [math.log(s["effect_size"]) for s in studies]
        ci_lowers = [math.log(s["ci_lower"]) for s in studies]
        ci_uppers = [math.log(s["ci_upper"]) for s in studies]
    else:
        effects = [s["effect_size"] for s in studies]
        ci_lowers = [s["ci_lower"] for s in studies]
        ci_uppers = [s["ci_upper"] for s in studies]

    ses = [log_se_from_ci(e, l, u, log_scale=False)
           for e, l, u in zip(effects, ci_lowers, ci_uppers)]

    # Fixed-effects
    fe_pooled, fe_se, fe_weights = pool_fixed_effects(effects, ses)
    fe_ci_lower = fe_pooled - 1.96 * fe_se
    fe_ci_upper = fe_pooled + 1.96 * fe_se

    # Random-effects (DerSimonian-Laird)
    re_pooled, re_se, re_weights, tau2, i2, Q, df = pool_random_effects_dl(effects, ses)
    re_ci_lower = re_pooled - 1.96 * re_se
    re_ci_upper = re_pooled + 1.96 * re_se

    # Convert back from log scale
    if log_scale:
        def back(x): return math.exp(x)
    else:
        def back(x): return x

    fe_result = {
        "pooled": back(fe_pooled),
        "ci_lower": back(fe_ci_lower),
        "ci_upper": back(fe_ci_upper),
        "p_value": z_to_p(fe_pooled / fe_se),
    }
    re_result = {
        "pooled": back(re_pooled),
        "ci_lower": back(re_ci_lower),
        "ci_upper": back(re_ci_upper),
        "p_value": z_to_p(re_pooled / re_se),
    }
    heterogeneity = {
        "Q": Q, "df": df, "Q_p_value": z_to_p(math.sqrt(max(0, Q - df))),
        "I2_pct": round(i2, 1),
        "tau2": round(tau2, 4),
        "interpretation": (
            "Low heterogeneity (I²<25%)" if i2 < 25 else
            "Moderate heterogeneity (25%≤I²<50%)" if i2 < 50 else
            "Substantial heterogeneity (50%≤I²<75%)" if i2 < 75 else
            "Considerable heterogeneity (I²≥75%)"
        ),
    }

    # Egger's test (only meaningful with ≥5 studies)
    egger = egger_test(effects, ses) if len(studies) >= 5 else {
        "note": f"Egger's test not run (n={len(studies)} studies; requires ≥5)"
    }

    # Per-study weights (% of total, random-effects)
    total_w = sum(re_weights)
    for s, w in zip(studies, re_weights):
        s["weight_pct"] = round(w / total_w * 100, 1)

    return {
        "pico_label": pico_label,
        "outcome_label": outcome_label,
        "measure_type": measure_type,
        "n_studies": len(studies),
        "fixed_effects": fe_result,
        "random_effects": re_result,
        "heterogeneity": heterogeneity,
        "egger_test": egger,
        "studies_with_weights": studies,
    }


# ── forest plot ───────────────────────────────────────────────────────────────

def draw_forest_plot(result, output_path="forest_plot.png"):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.patches as mpatches

    studies = result["studies_with_weights"]
    measure = result["measure_type"]
    log_scale = measure in ("OR", "RR", "HR")
    n = len(studies)

    fig, ax = plt.subplots(figsize=(10, max(4, n * 0.55 + 3)))

    # Rows: studies bottom-to-top
    y_positions = list(range(n, 0, -1))

    for i, (s, y) in enumerate(zip(studies, y_positions)):
        es = s["effect_size"]
        lo = s["ci_lower"]
        hi = s["ci_upper"]
        w = s.get("weight_pct", 1)

        # CI line
        ax.hlines(y, lo, hi, colors="black", linewidth=1.2)
        # Square (size proportional to weight)
        size = max(4, w * 0.5)
        ax.plot(es, y, "s", markersize=size, color="steelblue")
        # Label
        ax.text(-0.02 if not log_scale else 0.98, y,
                f"{s['study_id']}  {w:.1f}%",
                ha="right" if log_scale else "right",
                va="center", fontsize=8, transform=ax.get_yaxis_transform())

    # Pooled diamond (random-effects)
    re = result["random_effects"]
    diamond_x = [re["ci_lower"], re["pooled"], re["ci_upper"], re["pooled"]]
    diamond_y = [0.5, 0.8, 0.5, 0.2]
    ax.fill(diamond_x, diamond_y, color="darkred", zorder=5)

    # Reference line at 1 (ratio) or 0 (difference)
    ref = 1.0 if log_scale else 0.0
    ax.axvline(ref, color="gray", linestyle="--", linewidth=0.8)

    if log_scale:
        ax.set_xscale("log")

    ax.set_yticks([])
    ax.set_xlabel(f"{measure} (95% CI)", fontsize=10)
    ax.set_title(
        f"{result['pico_label']}\n"
        f"Outcome: {result['outcome_label']}  |  "
        f"RE pooled {measure}: {re['pooled']:.2f} ({re['ci_lower']:.2f}–{re['ci_upper']:.2f})  |  "
        f"I²={result['heterogeneity']['I2_pct']:.0f}%",
        fontsize=9, pad=8
    )

    re_patch = mpatches.Patch(color="darkred",
        label=f"RE pooled: {re['pooled']:.2f} ({re['ci_lower']:.2f}–{re['ci_upper']:.2f}), p={re['p_value']:.3f}")
    ax.legend(handles=[re_patch], loc="lower right", fontsize=8)

    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches="tight")
    plt.close()
    print(f"Forest plot saved to {output_path}")
    return output_path


# ── run and report ────────────────────────────────────────────────────────────

result = run_meta_analysis(studies, measure_type, outcome_label, pico_label)
plot_path = draw_forest_plot(result, output_path="forest_plot.png")

print(json.dumps(result, indent=2))
```

## Reading the Output

### Choosing Fixed vs Random Effects

- **Fixed-effects** assumes all studies estimate the same underlying effect (no between-study variance)
- **Random-effects (DL)** assumes the true effect varies across study populations — generally preferred for clinical meta-analyses
- When I² < 25%, results should be similar; when I² ≥ 50%, random-effects is strongly preferred

### Interpreting Heterogeneity

| I² range | Interpretation | Action |
|---|---|---|
| 0–25% | Low | Pool freely; fixed-effects acceptable |
| 25–50% | Moderate | Explore subgroups; prefer random-effects |
| 50–75% | Substantial | Investigate sources; consider not pooling |
| ≥75% | Considerable | Serious concern; narrative synthesis preferred |

If Q p-value < 0.05, heterogeneity is statistically significant (but Q is underpowered in small meta-analyses).

### Egger's Test

- Requires ≥5 studies for reliable interpretation
- p < 0.05 suggests funnel plot asymmetry, which may indicate publication bias
- Can also reflect small-study effects (sicker/different patients in small trials)

## GRADE Implications

Use meta-analysis results to inform GRADE certainty (see `dismech` grade-evidence skill):

| Meta-analysis result | GRADE impact |
|---|---|
| I² ≥ 50% | Downgrade one level for inconsistency |
| I² ≥ 75% | Downgrade two levels for inconsistency |
| CIs span the null + span clinical importance boundary | Downgrade for imprecision |
| Egger's p < 0.05 | Downgrade for publication bias (if large unexplained asymmetry) |
| Large pooled effect (OR/RR < 0.5 or > 2.0) | Consider upgrading observational evidence |

## Common Pitfalls

1. **Mixing outcomes** — never pool mortality with hospitalisation or surrogate endpoints
2. **Double-counting arms** — multi-arm trials must contribute only once per pairwise comparison
3. **Unit mismatch for MD** — ensure all studies use the same scale (e.g. all in mmHg)
4. **Ordinal outcomes treated as continuous** — use OR or relative odds for ordinal scales
5. **Time-to-event without accounting for censoring** — use HR, not RR, for survival data

## Reporting the Result

Summarise in one paragraph:

```
Meta-analysis of N studies (n=XXXX participants) showed that [intervention] 
significantly reduced [outcome] compared with [comparator]: 
RE pooled [measure_type] [value] (95% CI [lower]–[upper]), p=[p]; 
I²=[i2]% ([interpretation]). [Egger's test result if ≥5 studies.]
Forest plot saved to forest_plot.png.
```
