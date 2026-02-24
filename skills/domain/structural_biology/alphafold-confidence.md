---
name: alphafold-confidence
description: Interpreting AlphaFold pLDDT and PAE confidence metrics
---

# AlphaFold Confidence Metrics

AlphaFold provides per-residue confidence scores that indicate prediction reliability. Understanding these metrics is essential for interpreting structure comparisons.

## pLDDT (Predicted Local Distance Difference Test)

pLDDT measures per-residue confidence on a scale of 0-100.

### Interpreting pLDDT Values

- **pLDDT > 90**: Very high confidence
  - Prediction is highly reliable
  - Expect excellent agreement with experimental structures
  - Typically well-defined structural regions (helices, sheets)

- **pLDDT 70-90**: Confident
  - Good prediction quality
  - Usually correct backbone and often correct side-chains
  - Most of the structure falls in this range

- **pLDDT 50-70**: Low confidence
  - Uncertain predictions
  - Often corresponds to flexible loops, disordered regions
  - Backbone may be approximately correct but details unreliable
  - These regions often show high RMSD when compared to experiment

- **pLDDT < 50**: Very low confidence
  - Prediction is unreliable
  - Likely intrinsically disordered or no clear structure
  - Should not be interpreted as a reliable structure
  - May have missing density in experimental structures

## Using pLDDT in Analysis

### Extract confidence scores

```
parse_alphafold_confidence("alphafold_P04637.pdb")
```

This will show:
- Average pLDDT across the structure
- Regions with low confidence (pLDDT < 70)
- Summary statistics

### Cross-reference with RMSD

Low pLDDT regions that show high RMSD are expected - these are flexible or disordered regions where AlphaFold is uncertain and experimental structures may vary.

**This is normal biology, not a problem!**

High pLDDT regions that show high RMSD are more interesting:
- May indicate conformational changes (ligand binding, activation)
- Could be different biological states
- Might suggest prediction error (rare for high pLDDT)

### Visualize confidence

Use `execute_code` to create plots:

```python
# Plot pLDDT along sequence
import matplotlib.pyplot as plt

# Extract pLDDT from B-factor column
plddts = []
residues = []

with open('data/alphafold_P04637.pdb', 'r') as f:
    for line in f:
        if line.startswith('ATOM') and line[12:16].strip() == 'CA':
            residues.append(int(line[22:26].strip()))
            plddts.append(float(line[60:66].strip()))

plt.figure(figsize=(12, 4))
plt.plot(residues, plddts, linewidth=2)
plt.axhline(y=90, color='green', linestyle='--', alpha=0.5, label='High confidence')
plt.axhline(y=70, color='orange', linestyle='--', alpha=0.5, label='Low confidence cutoff')
plt.axhline(y=50, color='red', linestyle='--', alpha=0.5, label='Very low confidence')
plt.xlabel('Residue Number')
plt.ylabel('pLDDT Score')
plt.title('AlphaFold Confidence Along Sequence')
plt.legend()
plt.grid(alpha=0.3)
plt.tight_layout()
plt.savefig('plots/plddt_profile.png', dpi=150)
```

## PAE (Predicted Aligned Error)

PAE is a matrix showing expected error in relative positions between residue pairs.

### When to Use PAE

- Identifying domain boundaries
- Understanding multi-domain proteins
- Assessing relative domain orientations
- Detecting structured vs unstructured regions

### Interpreting PAE

- **Low PAE (< 5 Å)**: High confidence in relative positioning
- **High PAE (> 15 Å)**: Uncertain relative positioning (flexible linkers, independent domains)

If PAE JSON is available, load it:

```
parse_alphafold_confidence("alphafold_P04637.pdb", "alphafold_P04637_pae.json")
```

## Common Patterns

### Well-Folded Globular Proteins
- High average pLDDT (> 80)
- Most residues > 90
- Low pLDDT only in short loops

### Multi-Domain Proteins
- High pLDDT within domains
- Variable pLDDT in linkers
- PAE shows block structure

### Proteins with Disordered Regions
- Mixed pLDDT profile
- Long stretches of pLDDT < 50
- These regions may be missing in experimental structures

### Membrane Proteins
- Variable confidence (AlphaFold struggles with some membrane proteins)
- Transmembrane helices may have decent confidence
- Loops and termini often low confidence

## Integration with Structure Comparison

When analyzing experimental vs predicted structures:

1. **First:** Check pLDDT profile
2. **Then:** Compare structures
3. **Correlate:** High RMSD + low pLDDT = expected (flexible region)
4. **Investigate:** High RMSD + high pLDDT = interesting (conformational change?)

## Literature Context

Always search literature for:
- Known flexible regions
- Conformational states
- Functional importance of low-confidence regions
- Disorder predictions from other methods

Example searches:
```
search_pubmed("p53 intrinsically disordered regions")
search_pubmed("p53 flexible loop DNA binding")
search_pubmed("AlphaFold accuracy p53")
```

## Recording Findings

When you identify important confidence patterns:

```
update_knowledge_state(
    title="N-terminal domain shows low AlphaFold confidence",
    evidence="Residues 1-50: avg pLDDT = 45, known disordered region",
    interpretation="N-terminal transactivation domain is intrinsically disordered, explains missing density in crystal structures and high RMSD vs AlphaFold"
)
```

## Important Notes

- Low confidence ≠ bad prediction - it means AlphaFold is telling you the region is flexible/disordered
- High confidence predictions can still be wrong (but rarely)
- Always combine confidence with experimental validation and literature
- pLDDT correlates with experimental B-factors and disorder predictions
