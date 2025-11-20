# Manual Test Experiment: Phenix with TP53 Data

This experiment validates the core Phenix integration concept before implementation.

## Goal

Manually run Phenix tools on TP53 test data to:
1. Verify Phenix can compare experimental vs AlphaFold structures
2. Understand what outputs we get (RMSD, validation metrics)
3. Identify which tools are most useful for the autonomous agent
4. Test error handling (chain mismatches, file format issues)

## Prerequisites

```bash
# Set up Phenix environment
source /Applications/phenix-1.21.2-5419/phenix_env.sh

# Verify Phenix is working
phenix.python --version
```

## Experiment 1: Structure Superposition

**Goal:** Calculate RMSD between experimental and AlphaFold structures

```bash
cd test_data/tp53

# Compare structures
phenix.superpose_pdbs \
  3ts8.pdb \
  alphafold_P04637.pdb \
  > superpose_output.txt 2>&1

# Check output
cat superpose_output.txt
```

**Expected challenges:**
- 3ts8.pdb has 4 chains (tetramer), AlphaFold has 1 chain (monomer)
- Sequence alignment might be needed
- Different residue numbering

**What to observe:**
- Does it automatically pick matching chains?
- What's the RMSD value?
- Does it report per-residue deviations?
- What format is the output (parseable by agent)?

## Experiment 2: Extract Single Chain

**Goal:** Extract one chain from 3ts8 for better comparison

```bash
# List chains in 3ts8
grep "^ATOM" 3ts8.pdb | awk '{print $5}' | sort -u

# Extract chain A using phenix or simple grep
grep "^ATOM.*  A  " 3ts8.pdb > 3ts8_chainA.pdb
grep "^END" 3ts8.pdb >> 3ts8_chainA.pdb

# Now try superposition again
phenix.superpose_pdbs \
  3ts8_chainA.pdb \
  alphafold_P04637.pdb \
  > superpose_chainA_output.txt 2>&1

cat superpose_chainA_output.txt
```

**What to observe:**
- Better RMSD with single chain?
- Alignment quality scores
- Residue-by-residue RMSD values

## Experiment 3: Validation Metrics

**Goal:** Run validation tools on both structures

```bash
# Clashscore (steric clashes)
echo "=== Experimental structure clashscore ==="
phenix.clashscore 3ts8_chainA.pdb

echo "=== AlphaFold prediction clashscore ==="
phenix.clashscore alphafold_P04637.pdb

# Ramachandran validation
echo "=== Experimental structure Ramachandran ==="
phenix.cablam_validate 3ts8_chainA.pdb

echo "=== AlphaFold prediction Ramachandran ==="
phenix.cablam_validate alphafold_P04637.pdb
```

**What to observe:**
- Are validation scores different between experimental and predicted?
- What format are the outputs?
- Can we easily extract key metrics (scores, percentages)?

## Experiment 4: Extract AlphaFold Confidence

**Goal:** Read pLDDT from AlphaFold PDB B-factor column

```bash
# pLDDT is stored in the B-factor column of AlphaFold PDB files
# Extract residue number and pLDDT
echo "Residue  pLDDT"
grep "^ATOM.*  CA " alphafold_P04637.pdb | awk '{print $6, $11}'
```

**What to observe:**
- Which residues have low confidence (pLDDT < 70)?
- Do low-confidence regions correlate with high RMSD regions?

## Experiment 5: Compare Specific Regions

**Goal:** Identify which regions differ most

```bash
# If phenix.superpose_pdbs outputs per-residue RMSD, analyze it
# Otherwise, use Python to calculate per-residue distances

python3 << 'EOF'
from Bio.PDB import PDBParser, Superimposer
import numpy as np

# Parse structures
parser = PDBParser(QUIET=True)
exp_structure = parser.get_structure("exp", "3ts8_chainA.pdb")
af_structure = parser.get_structure("af", "alphafold_P04637.pdb")

# Get CA atoms
exp_atoms = [atom for atom in exp_structure.get_atoms() if atom.name == "CA"]
af_atoms = [atom for atom in af_structure.get_atoms() if atom.name == "CA"]

# Superimpose
super_imposer = Superimposer()
super_imposer.set_atoms(exp_atoms[:len(af_atoms)], af_atoms[:len(exp_atoms)])
super_imposer.apply(af_structure.get_atoms())

print(f"Global RMSD: {super_imposer.rms:.3f} Å")

# Per-residue distances
print("\nPer-residue distances:")
print("Residue  Distance(Å)")
for i, (exp_ca, af_ca) in enumerate(zip(exp_atoms[:len(af_atoms)], af_atoms[:len(exp_atoms)])):
    dist = np.linalg.norm(exp_ca.coord - af_ca.coord)
    if dist > 2.0:  # Flag high deviations
        print(f"{i+1:4d}     {dist:6.3f}  ***")
    else:
        print(f"{i+1:4d}     {dist:6.3f}")
EOF
```

**What to observe:**
- Which regions have highest deviation?
- Are there clusters of high-deviation residues?
- Do these correspond to functional regions (DNA binding, etc.)?

## Experiment 6: Literature Context

**Goal:** See what PubMed says about p53 flexibility

```bash
# This you'd do via web browser or API, but for planning:
# Search queries to try:
# - "p53 flexible regions crystal structure"
# - "p53 DNA binding conformational change"
# - "p53 AlphaFold prediction accuracy"
```

**What to observe:**
- Are there known flexible regions?
- Do papers discuss conformational changes?
- Is there literature on p53 AlphaFold accuracy?

## Expected Findings

Based on 2024 literature, you should observe:

1. **RMSD:** Likely 2-4 Å overall (moderate differences)
2. **Flexible regions:** Loops and DNA-binding regions may differ
3. **AlphaFold confidence:** Some regions will have pLDDT < 70
4. **Validation:** AlphaFold might have better geometry (no experimental constraints)
5. **Conformational state:** 3ts8 is DNA-bound, AlphaFold predicts apo state

## Document Your Results

Create a file `manual_test_results.txt` with:
```
=== Experiment Results ===

Experiment 1 (Superposition):
- RMSD: [value] Å
- Issues encountered: [list]
- Output format: [description]

Experiment 2 (Single chain):
- RMSD: [value] Å
- Better than multi-chain? [yes/no]

Experiment 3 (Validation):
- Experimental clashscore: [value]
- AlphaFold clashscore: [value]
- Key differences: [notes]

Experiment 4 (Confidence):
- Low confidence regions: [residue ranges]
- Average pLDDT: [value]

Experiment 5 (Per-residue):
- Highest deviation regions: [list]
- Correlation with pLDDT? [yes/no]

Experiment 6 (Literature):
- Key papers found: [list]
- Relevant findings: [notes]
```

## Next Steps

After running this experiment:
1. Share results to inform implementation priorities
2. Identify which Phenix tools are most useful
3. Understand output formats for parsing
4. Refine MCP tool designs based on real outputs
5. Test error cases (missing chains, format issues)

## Running the Experiment

```bash
# From shandy root directory
cd test_data/tp53

# Source Phenix
source /Applications/phenix-1.21.2-5419/phenix_env.sh

# Run experiments 1-5 above
# Document results in manual_test_results.txt
```
