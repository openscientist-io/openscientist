---
name: interpreting-discrepancies
description: Framework for explaining structural differences between experimental and predicted structures
---

# Interpreting Structural Discrepancies

When experimental and AlphaFold structures disagree, systematic investigation reveals whether differences are errors, biology, or methodology.

## Framework for Analysis

### 1. Quantify the Discrepancy

**Global assessment:**
```
compare_structures(
    experimental_pdb="experimental.pdb",
    predicted_pdb="alphafold.pdb"
)
```

**Regional assessment:**
```python
# Use execute_code to calculate per-residue RMSD
from Bio.PDB import PDBParser, Superimposer
import numpy as np
# ... calculate distances for each residue
```

### 2. Check Prediction Confidence

```
parse_alphafold_confidence("alphafold.pdb")
```

**Ask:** Does low pLDDT explain the discrepancy?
- If yes → Expected (flexible/disordered region)
- If no → More interesting (investigate further)

### 3. Validate Structure Quality

```
run_phenix_tool("phenix.clashscore", ["experimental.pdb"])
run_phenix_tool("phenix.clashscore", ["alphafold.pdb"])
```

**Ask:** Do both structures have reasonable quality?
- If no → One may have errors
- If yes → Likely real conformational difference

### 4. Search Literature

```
search_pubmed("[protein name] flexible regions")
search_pubmed("[protein name] conformational change")
search_pubmed("[protein name] crystal structure dynamics")
```

**Look for:**
- Known flexible regions
- Ligand-induced changes
- Active/inactive states
- NMR or MD studies showing dynamics

### 5. Formulate Hypothesis

Based on Steps 1-4, generate testable hypothesis about the cause.

### 6. Test Hypothesis

Design additional analyses to support or refute your hypothesis.

### 7. Record Finding

```
update_knowledge_state(
    title="[Concise description of discrepancy and cause]",
    evidence="[Quantitative data supporting your conclusion]",
    interpretation="[Biological meaning]"
)
```

## Common Causes and How to Identify Them

### Cause 1: Flexible/Disordered Regions

**Signatures:**
- Low AlphaFold pLDDT (< 70)
- High experimental B-factors
- Missing density in experimental structure
- Literature describes region as "flexible loop" or "disordered"

**Example:**
```
High RMSD in residues 150-165
+ pLDDT 55 in this region
+ Literature: "N-terminal loop is disordered"
→ Conclusion: Expected discrepancy, flexible region
```

**Biological Meaning:** Often functionally important (binding sites, regulatory regions)

### Cause 2: Ligand-Induced Conformational Change

**Signatures:**
- High pLDDT in discrepant region
- Experimental structure has bound ligand
- AlphaFold predicts apo (unbound) state
- Literature describes conformational change upon binding

**Example:**
```
RMSD 4.2 Å in DNA-binding domain
+ pLDDT 92 in this region (high confidence)
+ Experimental structure has bound DNA
+ Literature: "DNA binding causes helix rotation"
→ Conclusion: Different conformational states
```

**Biological Meaning:** Reveals functional mechanism

### Cause 3: Domain Orientation Differences

**Signatures:**
- Multi-domain protein
- Good alignment within individual domains
- Poor alignment of relative orientations
- PAE shows uncertain inter-domain positioning
- Literature describes domain movements

**Example:**
```
Domains A and B individually align well (RMSD < 1 Å)
+ Overall RMSD 6 Å
+ PAE high between domains
+ Literature: "Hinge region allows domain rotation"
→ Conclusion: Different domain orientations
```

**Biological Meaning:** May represent different functional states or conformational freedom

### Cause 4: Crystal Packing Artifacts

**Signatures:**
- Discrepancy at crystal contacts
- Non-physiological conditions (pH, salt)
- Multiple structures show different conformations
- Solution studies (NMR, SAXS) disagree with crystal

**Example:**
```
C-terminal helix differs in crystal structure
+ Forms dimer interface in crystal
+ AlphaFold predicts monomer
+ Literature: "Protein is monomeric in solution"
→ Conclusion: Crystal artifact
```

**Biological Meaning:** AlphaFold may be more biologically relevant

### Cause 5: Missing Post-Translational Modifications

**Signatures:**
- AlphaFold doesn't model PTMs (phosphorylation, glycosylation, etc.)
- Experimental structure has modifications
- Discrepancy at modification sites
- Literature describes functional PTMs

**Example:**
```
Loop conformation differs near Ser45
+ Experimental structure has phospho-Ser45
+ Literature: "Phosphorylation regulates activity"
→ Conclusion: PTM-induced change
```

**Biological Meaning:** Regulatory mechanism

### Cause 6: AlphaFold Prediction Error

**Signatures:**
- High pLDDT but clearly wrong
- Novel fold not in training data
- Requires cofactors AlphaFold doesn't know about
- Multiple experimental structures agree, AlphaFold differs

**Example:**
```
Active site geometry wrong in AlphaFold
+ Metal-binding site predicted incorrectly
+ Multiple crystal structures agree
+ pLDDT 85 (high but wrong)
→ Conclusion: AlphaFold error (missing metal information)
```

**Biological Meaning:** Limits of prediction

### Cause 7: Experimental Structure Error/Uncertainty

**Signatures:**
- Low resolution experimental structure (> 3 Å)
- Poor validation metrics (high clashscore, Ramachandran outliers)
- Missing or ambiguous density
- AlphaFold may be more reliable

**Example:**
```
Experimental structure has clashscore 25
+ Resolution 3.8 Å
+ Region has missing density
+ AlphaFold pLDDT 90, good geometry
→ Conclusion: Experimental uncertainty, AlphaFold may be better
```

## Hypothesis Testing Strategies

### If you suspect flexibility:
- Check B-factors in experimental structure
- Search for NMR or MD studies
- Look for "disorder" predictions

### If you suspect conformational change:
- Compare ligand/cofactor presence
- Search for "active site" or "allosteric"
- Look for other structures in different states

### If you suspect experimental artifacts:
- Check resolution and validation
- Look for other experimental structures
- Search for solution studies (NMR, SAXS)

### If you suspect prediction errors:
- Check for unusual features (rare fold, cofactors, membranes)
- Compare with homologs
- Look for experimental validation studies of AlphaFold

## Example Analysis

```
# 1. Initial comparison
compare_structures("3ts8.pdb", "alphafold_P04637.pdb")
# Result: RMSD 3.2 Å

# 2. Check confidence
parse_alphafold_confidence("alphafold_P04637.pdb")
# Result: Mixed - some regions pLDDT 90+, others 60-70

# 3. Identify specific regions
execute_code(code='''
# Calculate per-residue deviations
# Plot RMSD vs pLDDT
''', description="Per-residue analysis")
# Result: Residues 150-165 have high RMSD (4.5 Å) and pLDDT 62

# 4. Literature search
search_pubmed("p53 flexible loop residues 150-165")
# Result: Papers describe this as "DNA-binding loop, flexible in apo state"

# 5. Formulate hypothesis
# Hypothesis: Residues 150-165 differ because 3ts8 has bound DNA,
# AlphaFold predicts apo state, and this loop is flexible

# 6. Test hypothesis
execute_code(code='''
# Check if 3ts8 has DNA bound
# Extract residue 150-165 from both structures
''')
# Result: Confirms DNA present in 3ts8

search_pubmed("p53 DNA binding conformational change")
# Result: Multiple papers confirm loop ordering upon DNA binding

# 7. Record finding
update_knowledge_state(
    title="Residues 150-165 show conformational change upon DNA binding",
    evidence="RMSD 4.5 Å in loop, pLDDT 62, experimental structure DNA-bound",
    interpretation="DNA-binding loop is flexible in apo state (AlphaFold) but becomes ordered when bound to DNA (3ts8). This is expected and biologically relevant."
)
```

## Decision Tree Summary

```
Found high RMSD between structures?
│
├─ Low pLDDT in region?
│  └─ Yes → Likely flexible/disordered (expected)
│
├─ Different ligands/PTMs?
│  └─ Yes → Likely conformational change (interesting!)
│
├─ Multi-domain protein?
│  └─ Yes → Check domain orientations (may vary)
│
├─ Poor experimental validation?
│  └─ Yes → Possible experimental error (be cautious)
│
├─ Novel fold/cofactors?
│  └─ Yes → Possible AlphaFold limitation
│
└─ Literature reports dynamics?
   └─ Yes → Real biological flexibility
```

## Important Principles

1. **Both structures can be "right"** - they may represent different states
2. **Neither may be fully right** - both have limitations
3. **Discrepancies are scientifically valuable** - they reveal biology
4. **Always seek external evidence** - literature, other structures, biophysics
5. **Quantify uncertainty** - don't overinterpret marginal differences

## Common Mistakes to Avoid

❌ Assuming AlphaFold is always right
❌ Assuming experimental structure is always right
❌ Ignoring confidence scores
❌ Not searching literature
❌ Over-interpreting small differences (< 2 Å)
❌ Forgetting about biological context
❌ Treating all discrepancies as errors

## Final Checklist

Before concluding your analysis:

- [ ] Quantified discrepancy (global and regional RMSD)
- [ ] Checked AlphaFold confidence scores
- [ ] Validated both structure qualities
- [ ] Searched relevant literature
- [ ] Formulated specific hypothesis
- [ ] Tested hypothesis with additional data
- [ ] Considered biological meaning
- [ ] Recorded finding with evidence
