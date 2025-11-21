# TP53 Test Case for SHANDY-Phenix Integration

## Overview

Test case for comparing experimental TP53 (p53 tumor suppressor) crystal structure with AlphaFold prediction to identify and explain structural discrepancies.

## Files

### Experimental Structure (PDB ID: 3TS8)
- `3ts8.cif` - mmCIF format (929 KB)
- `3ts8.pdb` - PDB format (737 KB)
- **Description**: Crystal structure of multidomain human p53 tetramer bound to the natural CDKN1A (p21) p53-response element
- **Source**: RCSB Protein Data Bank
- **URL**: https://www.rcsb.org/structure/3TS8

### AlphaFold Prediction (UniProt: P04637)
- `alphafold_P04637.pdb` - Full-length p53 prediction (248 KB)
- `alphafold_P04637_pae.json` - Predicted aligned error matrix (412 KB)
- **Description**: AlphaFold v2 prediction for human cellular tumor antigen p53
- **Sequence length**: 393 residues
- **Average pLDDT**: 75.06 (High confidence)
- **Source**: AlphaFold Protein Structure Database
- **URL**: https://alphafold.ebi.ac.uk/entry/P04637

## Use Case

This test case demonstrates SHANDY-Phenix's ability to:

1. **Compare structures**: Align experimental vs. predicted structures
2. **Identify discrepancies**: Find regions where AlphaFold disagrees with experimental data
3. **Analyze confidence**: Use pLDDT and PAE data to understand prediction reliability
4. **Search literature**: Query PubMed for explanations of discrepancies
5. **Generate hypotheses**: Explain why differences occur (flexible regions, ligand binding, conformational states)

## Expected Findings

Based on literature (2024), TP53/p53 is known to have:
- Multiple biologically relevant conformations
- DNA-binding domains with conformational changes upon binding
- Flexible regions with low AlphaFold confidence
- Well-studied tumor suppressor with rich literature

This makes it an excellent test case for iterative autonomous exploration of structure-function relationships.

## Running the Test

```bash
# After SHANDY-Phenix implementation:
python -m shandy.web_app

# Upload: 3ts8.pdb (experimental) and alphafold_P04637.pdb (prediction)
# Research question: "Compare experimental p53 structure with AlphaFold prediction and explain structural discrepancies"
# Set iterations: 10
# Enable skills: Yes
```

### Exact Research Question

**"Compare experimental p53 structure with AlphaFold prediction and explain structural discrepancies"**
