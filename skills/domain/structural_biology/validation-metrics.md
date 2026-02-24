---
name: validation-metrics
description: Understanding Phenix validation scores for structure quality
---

# Phenix Validation Metrics

Phenix provides various validation tools to assess protein structure quality. Understanding these metrics helps evaluate both experimental and predicted structures.

## Clashscore

Measures steric clashes (atoms too close together).

### Running Clashscore

```
run_phenix_tool(
    tool_name="phenix.clashscore",
    input_files=["3ts8.pdb"],
    description="Checking for steric clashes in experimental structure"
)
```

### Interpreting Clashscore

- **Score < 5**: Excellent (crystallographic quality)
- **Score 5-10**: Good
- **Score 10-20**: Acceptable
- **Score > 20**: Poor, likely errors

AlphaFold predictions often have very low clashscores (< 5) because they're optimized for good geometry. Experimental structures may have higher scores due to:
- Refinement challenges
- Disorder/flexibility
- Crystal contacts

**Low clashscore doesn't mean the structure is biologically correct** - it just means good geometry.

## Ramachandran Analysis

Validates backbone torsion angles (phi/psi).

### Running Ramachandran Validation

```
run_phenix_tool(
    tool_name="phenix.cablam_validate",
    input_files=["alphafold_P04637.pdb"],
    description="Backbone validation of AlphaFold prediction"
)
```

### Interpreting Results

Good structures have:
- **> 95% in favored regions**
- **< 1% in outlier regions**

Outliers may indicate:
- Unusual but correct conformations (check literature)
- Errors in model
- Functional conformational strain

AlphaFold typically has excellent Ramachandran statistics.

## Geometry Validation

Checks bond lengths, angles, and other stereochemical properties.

Phenix refinement tools report:
- Bond RMSD
- Angle RMSD
- Planarity deviations

Ideal values are close to target geometry from chemical databases.

## Comparing Validation Metrics

When comparing experimental vs predicted structures:

```
# Experimental
run_phenix_tool("phenix.clashscore", ["3ts8.pdb"], description="Experimental validation")

# AlphaFold
run_phenix_tool("phenix.clashscore", ["alphafold_P04637.pdb"], description="AlphaFold validation")
```

### Expected Patterns

**AlphaFold typically has better validation scores:**
- Lower clashscores
- Better Ramachandran statistics
- Ideal bond lengths/angles

**This doesn't mean AlphaFold is more accurate biologically!**

It means:
- AlphaFold is optimized for good geometry
- Experimental structures reflect real-world constraints (crystal packing, disorder, refinement)
- Experimental structures may have biologically relevant strain or unusual geometries

## When to Use Validation Tools

1. **Initial assessment**: Check both structures have reasonable quality
2. **Troubleshooting**: If RMSD is high, check if one structure has major errors
3. **Identifying problems**: Find specific residues with poor geometry
4. **Hypothesis testing**: Are differences due to poor geometry or biology?

## Integration with Structure Comparison

If you find:
- **High RMSD + poor validation** in experimental structure → Possible experimental error or refinement issue
- **High RMSD + good validation** in both → Likely conformational difference (interesting!)
- **Low RMSD + poor validation** → Check for consistent errors

## Recording Validation Results

```
update_knowledge_state(
    title="Both structures show good validation metrics",
    evidence="Experimental clashscore: 8.2, AlphaFold clashscore: 2.1. Both >98% Ramachandran favored",
    interpretation="High RMSD (3.2 Å) is not due to poor quality - likely conformational differences"
)
```

## Other Useful Validation Tools

**phenix.chain_comparison**: Compare specific chains
```
run_phenix_tool("phenix.chain_comparison", ["structure1.pdb", "structure2.pdb"])
```

**phenix.comparama**: Detailed Ramachandran comparison
```
run_phenix_tool("phenix.comparama", ["3ts8.pdb"])
```

## Important Principles

1. **Good validation ≠ correct biology**: A structure can have perfect geometry but be in the wrong conformation
2. **Poor validation ≠ useless**: Experimental structures with some validation issues may still be biologically relevant
3. **Context matters**: Always interpret validation in light of resolution, experimental conditions, and biological function
4. **Compare fairly**: Different structure determination methods have different typical validation profiles

## Common Validation Issues

**Missing density (experimental):**
- Flexible regions may have poor validation
- B-factors will be high
- May be legitimately disordered

**Crystal contacts:**
- May cause unusual geometry
- Not biologically relevant
- Compare with solution structures or predictions

**Refinement artifacts:**
- Automated refinement may introduce errors
- Manual inspection often helps
- Literature may report known issues

## Decision Tree

```
Is RMSD high between structures?
├─ Yes
│  ├─ Both have good validation?
│  │  └─ → Likely conformational difference (investigate biology)
│  └─ One has poor validation?
│     └─ → Possible error in that structure (check literature, resolution)
└─ No (RMSD low)
   └─ Good agreement! Still check validation to confirm quality
```
