---
name: comparing-structures
description: How to align and compare experimental vs predicted protein structures
---

# Comparing Protein Structures

When comparing experimental and predicted structures, use a systematic approach to identify and interpret differences.

## Initial Comparison

Use the `compare_structures` tool with experimental and predicted PDB files:

```
compare_structures(
    experimental_pdb="3ts8.pdb",
    predicted_pdb="alphafold_P04637.pdb",
    description="Initial alignment of experimental vs AlphaFold p53 structures"
)
```

This runs `phenix.superpose_pdbs` and reports:
- Global RMSD (Å)
- Alignment quality
- Number of aligned residues

## Interpreting RMSD

Use these guidelines to understand RMSD values:

- **RMSD < 1.0 Å**: Excellent agreement - structures are nearly identical
- **RMSD 1-2 Å**: Good agreement with minor differences
  - Typical for high-quality predictions of well-defined structures
  - Small side-chain rotations, minor loop adjustments
- **RMSD 2-4 Å**: Moderate differences - investigate specific regions
  - May indicate flexible regions, domain movements, or conformational changes
  - Worth detailed analysis to understand causes
- **RMSD > 4 Å**: Significant differences
  - Likely different conformational states
  - Possible errors in prediction or experimental structure
  - May represent biologically relevant differences

## Finding Problematic Regions

After global alignment, identify specific regions with high deviation:

1. **Check for multi-chain structures**
   - Experimental structures may have multiple chains (dimers, tetramers)
   - AlphaFold predictions are typically monomers
   - Extract single chains for better comparison if needed

2. **Look for flexible loops**
   - Cross-reference high RMSD regions with AlphaFold confidence (pLDDT < 70)
   - Low confidence often correlates with flexible, disordered regions

3. **Consider secondary structure**
   - Are differences in loops, helices, or sheets?
   - Structured regions (helices/sheets) should generally agree better

4. **Check for missing density**
   - Experimental structures may have missing residues due to disorder
   - Compare sequence coverage between experimental and predicted

## Common Causes of Discrepancies

**Flexible loops and disordered regions:**
- Low AlphaFold confidence (pLDDT < 70)
- High experimental B-factors
- May have missing density in experimental structure
- These are expected and often functionally important

**Conformational changes:**
- Ligand binding (experimental bound, AlphaFold apo)
- Activation states (active vs inactive)
- pH or crystallization conditions

**Domain orientations:**
- Multi-domain proteins can have different relative orientations
- Hinges and linkers allow movement
- AlphaFold may not capture all possible states

**Crystal contacts or artifacts:**
- Experimental structure may show non-physiological conformations
- Crystal packing can distort flexible regions

**Prediction errors:**
- AlphaFold can be wrong, especially for:
  - Novel folds not well-represented in training data
  - Proteins requiring cofactors or metal ions
  - Intrinsically disordered proteins

## Recommended Workflow

1. Run initial `compare_structures` to get global RMSD
2. Use `parse_alphafold_confidence` to identify low-confidence regions
3. Run validation tools (`run_phenix_tool` with `phenix.clashscore`, etc.)
4. Use `execute_code` to calculate per-residue deviations and visualize
5. Search literature for known flexible regions or conformational states
6. Formulate hypotheses about why specific regions differ
7. Test hypotheses with additional analyses
8. Record confirmed findings to knowledge graph

## Example Analysis Pattern

```
# 1. Initial comparison
compare_structures(...)

# 2. Check confidence
parse_alphafold_confidence("alphafold_P04637.pdb")

# 3. If RMSD > 2 Å, investigate per-residue
execute_code(
    code='''
    # Calculate per-residue RMSD using Biopython
    from Bio.PDB import PDBParser, Superimposer
    import numpy as np
    ...
    ''',
    description="Calculating per-residue deviations"
)

# 4. Search for biological context
search_pubmed("p53 flexible regions crystal structure")

# 5. Form hypothesis and record
update_knowledge_state(
    title="Residues 150-180 show high RMSD due to flexible loop",
    evidence="RMSD 3.5 Å in this region, pLDDT < 65, literature confirms flexibility",
    interpretation="This loop is unstructured in apo state but ordered when bound to DNA"
)
```

## Important Notes

- Don't expect perfect agreement - AlphaFold predicts one state, experiments may capture another
- Both high and low RMSD can be scientifically interesting
- Always consider biological context from literature
- Correlation with confidence scores helps distinguish prediction errors from real biology
