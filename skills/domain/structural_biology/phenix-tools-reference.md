---
name: phenix-tools-reference
description: Reference of available Phenix commands for structural biology analysis
---

# Phenix Tools Reference

The `run_phenix_tool` tool can execute any Phenix command. This reference lists the most useful commands grouped by task. All commands accept PDB or mmCIF files.

## Validation & Quality Assessment

| Command | Purpose |
|---------|---------|
| `phenix.molprobity` | Comprehensive validation: Ramachandran, rotamers, clashes, C-beta deviations — the single best "overall quality" check |
| `phenix.clashscore` | All-atom steric clash analysis |
| `phenix.ramalyze` | Ramachandran (phi/psi backbone) analysis |
| `phenix.rotalyze` | Side-chain rotamer analysis |
| `phenix.cablam_validate` | C-alpha based backbone validation |
| `phenix.cbetadev` | C-beta deviation analysis (detects modeling errors) |
| `phenix.omegalyze` | Cis/trans peptide bond validation |
| `phenix.model_vs_data` | Model vs diffraction data statistics (R-factors, resolution) |
| `phenix.model_statistics` | Summary geometry statistics for a model |
| `phenix.emringer` | Map-model validation for cryo-EM structures |
| `phenix.validation_cryoem` | Comprehensive cryo-EM validation |
| `phenix.undowser_validation` | Check waters for clashes and poor contacts |

### Example: Full validation

```python
run_phenix_tool(
    tool_name="phenix.molprobity",
    input_files=["structure.pdb"],
    description="Comprehensive structure quality check"
)
```

## Structure Comparison & Superposition

| Command | Purpose |
|---------|---------|
| `phenix.superpose_pdbs` | Superpose two structures, report RMSD (also available via `compare_structures` tool) |
| `phenix.chain_comparison` | Chain-level comparison between structures |
| `phenix.structure_comparison` | Broader structural comparison |
| `phenix.model_model_distances` | Per-residue distance between two models |
| `phenix.superpose_and_morph` | Superpose and morph one structure onto another |

### Example: Per-residue distances

```python
run_phenix_tool(
    tool_name="phenix.model_model_distances",
    input_files=["experimental.pdb", "predicted.pdb"],
    description="Per-residue distances between experimental and predicted"
)
```

## AlphaFold & Predicted Models

| Command | Purpose |
|---------|---------|
| `phenix.process_predicted_model` | Process AlphaFold/predicted structures (trim low-confidence regions, convert B-factors) |
| `phenix.dock_predicted_model` | Dock a predicted model into a cryo-EM map |

### Example: Process AlphaFold model

```python
run_phenix_tool(
    tool_name="phenix.process_predicted_model",
    input_files=["alphafold_model.pdb"],
    arguments={"pae_json_file_name": "alphafold_pae.json"},
    description="Process AlphaFold model, trim low-confidence regions"
)
```

## Refinement

| Command | Purpose |
|---------|---------|
| `phenix.refine` | Reciprocal-space refinement against diffraction data |
| `phenix.real_space_refine` | Real-space refinement (primarily for cryo-EM) |
| `phenix.geometry_minimization` | Energy minimization (fix geometry without data) |
| `phenix.dynamics` | Molecular dynamics refinement |

**Note:** Refinement commands are compute-intensive and may approach the 5-minute timeout. Use targeted refinement (specific chains/residues) when possible.

## Map Operations (Cryo-EM / Crystallography)

| Command | Purpose |
|---------|---------|
| `phenix.maps` | Compute electron density map coefficients |
| `phenix.map_box` | Extract map region around a model |
| `phenix.map_model_cc` | Map-model correlation coefficient |
| `phenix.mtriage` | Cryo-EM map analysis (resolution, sharpening) |
| `phenix.local_resolution` | Local resolution estimation |
| `phenix.auto_sharpen` | Map sharpening |
| `phenix.map_to_model` | Build atomic model from a cryo-EM map |
| `phenix.dock_in_map` | Dock a model into a map |
| `phenix.segment_and_split_map` | Segment map into domains |

## Model Building & Manipulation

| Command | Purpose |
|---------|---------|
| `phenix.autobuild` | Automated model building into density |
| `phenix.fit_loops` | Fit/rebuild loop regions |
| `phenix.pdbtools` | PDB manipulation (select atoms, modify B-factors, extract chains) |
| `phenix.reduce` | Add hydrogens to a structure |
| `phenix.ready_set` | Add hydrogens and generate ligand restraints |
| `phenix.find_helices_strands` | Identify secondary structure elements |

### Example: Extract a single chain

```python
run_phenix_tool(
    tool_name="phenix.pdbtools",
    input_files=["multimer.pdb"],
    arguments={"selection": '"chain A"', "output.file_name": "chain_A.pdb"},
    description="Extract chain A from multimer"
)
```

## Ligand Tools

| Command | Purpose |
|---------|---------|
| `phenix.elbow` | Generate ligand geometry and restraints from SMILES or PDB |
| `phenix.ligandfit` | Fit a ligand into electron density |
| `phenix.ligand_identification` | Identify unknown ligand density |
| `phenix.find_all_ligands` | Find all ligand binding sites |

## Data Analysis

| Command | Purpose |
|---------|---------|
| `phenix.xtriage` | Diffraction data analysis (twinning, anisotropy, resolution) |
| `phenix.merging_statistics` | Data merging statistics |
| `phenix.french_wilson` | French-Wilson scaling |
| `phenix.cif_as_mtz` / `phenix.mtz_as_cif` | Reflection file format conversion |
| `phenix.pdb_as_cif` / `phenix.cif_as_pdb` | Model file format conversion |

## Molecular Replacement

| Command | Purpose |
|---------|---------|
| `phenix.phaser` | Molecular replacement |
| `phenix.ensembler` | Prepare search ensembles for MR |
| `phenix.sculptor` | Edit search models for MR |
| `phenix.mr_model_preparation` | Prepare MR search models |

## Sequence & Annotation

| Command | Purpose |
|---------|---------|
| `phenix.print_sequence` | Extract sequence from PDB |
| `phenix.model_vs_sequence` | Compare model against expected sequence |
| `phenix.fetch_pdb` | Download PDB entries by ID |

## Useful Utilities

| Command | Purpose |
|---------|---------|
| `phenix.b_factor_statistics` | B-factor distribution analysis |
| `phenix.find_ncs` | Detect non-crystallographic symmetry |
| `phenix.hbond` | Hydrogen bond analysis |
| `phenix.table_one` | Generate "Table 1" statistics for publication |

## Tips for Using `run_phenix_tool`

1. **Start with `phenix.molprobity`** for a quick quality overview of any structure
2. **Input files are relative to `data/`** — just use the filename, not the full path
3. **Pass arguments as a dict**: `arguments={"flag": "value"}`
4. **5-minute timeout** — avoid running refinement on very large structures
5. **Check `phenix.pdbtools`** for any PDB manipulation task before writing custom code
6. **Format conversion**: Use `phenix.pdb_as_cif` or `phenix.cif_as_pdb` if your files are in the wrong format
