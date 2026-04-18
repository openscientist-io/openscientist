---
name: phenix-tools-reference
description: Reference of available Phenix commands for structural biology analysis
category: domain
---

# Phenix Tools Reference

If Phenix is available, prefer `run_phenix_tool` over `execute_code` for structural biology tasks like validation, superposition, refinement, and map analysis. Phenix is the gold standard for these tasks.

Call `run_phenix_tool(tool_name="phenix.<command>", input_files=["file.pdb"], description="...")`. This reference lists the most useful commands grouped by task. All commands accept PDB or mmCIF files.

## Validation and Quality Assessment

| Command | Purpose |
|---------|---------|
| `phenix.molprobity` | Comprehensive validation: Ramachandran, rotamers, clashes, C-beta deviations; the single best overall quality check |
| `phenix.clashscore` | All-atom steric clash analysis |
| `phenix.ramalyze` | Ramachandran backbone analysis |
| `phenix.rotalyze` | Side-chain rotamer analysis |
| `phenix.cablam` | C-alpha based backbone validation |
| `phenix.cbetadev` | C-beta deviation analysis |
| `phenix.omegalyze` | Cis/trans peptide bond validation |
| `phenix.model_vs_data` | Model versus diffraction data statistics |
| `phenix.model_statistics` | Summary geometry statistics for a model |
| `phenix.emringer` | Map-model validation for cryo-EM structures |
| `phenix.validation_cryoem` | Comprehensive cryo-EM validation |
| `phenix.undowser_validation` | Check waters for clashes and poor contacts |
| `phenix.clashscore2` | Updated all-atom clash score (prefer over `clashscore` when available; slightly different scoring) |
| `phenix.undowser2_validation` | Updated water validation (same relationship to `undowser_validation` as above) |
| `phenix.holton_geometry_validation` | Holton-method geometry validation — complementary signal to molprobity; useful for second-opinion checks |

### Example: Full validation

```python
run_phenix_tool(
    tool_name="phenix.molprobity",
    input_files=["structure.pdb"],
    description="Comprehensive structure quality check",
)
```

## Structure Comparison and Superposition

| Command | Purpose |
|---------|---------|
| `phenix.superpose_pdbs` | Superpose two structures and report RMSD |
| `phenix.chain_comparison` | Chain-level comparison between structures |
| `phenix.structure_comparison` | Broader structural comparison |
| `phenix.model_model_distances` | Per-residue distance between two models |
| `phenix.superpose_and_morph` | Superpose and morph one structure onto another |
| `phenix.superpose_models` | Superpose with optional morphing and trimming (more flexible variant of `superpose_and_morph`) |
| `phenix.find_reference` | Find reference models (e.g., homologs in the PDB) for a supplied model |

### Example: Per-residue distances

```python
run_phenix_tool(
    tool_name="phenix.model_model_distances",
    input_files=["experimental.pdb", "predicted.pdb"],
    description="Per-residue distances between experimental and predicted",
)
```

## AlphaFold and Predicted Models

| Command | Purpose |
|---------|---------|
| `phenix.process_predicted_model` | Process AlphaFold or predicted structures |
| `phenix.dock_predicted_model` | Dock a predicted model into a cryo-EM map |

### Example: Process AlphaFold model

```python
run_phenix_tool(
    tool_name="phenix.process_predicted_model",
    input_files=["alphafold_model.pdb"],
    arguments={"pae_json_file_name": "alphafold_pae.json"},
    description="Process AlphaFold model and trim low-confidence regions",
)
```

## Refinement

| Command | Purpose |
|---------|---------|
| `phenix.refine` | Reciprocal-space refinement against diffraction data |
| `phenix.real_space_refine` | Real-space refinement, primarily for cryo-EM |
| `phenix.geometry_minimization` | Energy minimization without data |
| `phenix.dynamics` | Molecular dynamics refinement |
| `phenix.fit_h` | Fit hydrogen positions with rotational DOF into a map (use after `phenix.reduce` when H positions matter for interpretation) |
| `phenix.rocket` | Wrapper for ROCKET refinement (external tool — see rocket-9.gitbook.io for docs) |
| `phenix.aquaref` | Quantum-mechanical (QM) refinement via qr.refine — specialized; only use when QM restraints are specifically required |
| `phenix.mopac` | Semiempirical QM refinement via MOPAC — specialized; same caveat as `aquaref` |
| `phenix.magref` | Magnetic / spin-dependent refinement — specialized; only for data with magnetic scattering |
| `phenix.TAAM_minus_IAM` | Difference between Transferable Aspherical Atom Model and Independent Atom Model refinements — specialized, for ultra-high-resolution data only |

Refinement commands are compute-intensive and may approach the 5-minute timeout. Use targeted refinement when possible. The last four rows above are narrow-use — do not invoke unless the task explicitly calls for QM/magnetic/aspherical refinement.

## Map Operations

| Command | Purpose |
|---------|---------|
| `phenix.maps` | Compute electron density map coefficients |
| `phenix.map_box` | Extract map region around a model |
| `phenix.map_model_cc` | Map-model correlation coefficient |
| `phenix.map_correlations` | Correlation between two maps, or map vs model (use when comparing maps to each other — `map_model_cc` is map-vs-model only) |
| `phenix.map_sharpening` | Map sharpening via scale-factor optimization (newer, more flexible than `auto_sharpen`; supports half-maps and model-guided modes) |
| `phenix.reduce_cryoem_resolution` | Artificially limit cryo-EM half-maps to a target resolution (for testing resolution dependence) |
| `phenix.mtriage` | Cryo-EM map analysis |
| `phenix.local_resolution` | Local resolution estimation |
| `phenix.auto_sharpen` | Map sharpening |
| `phenix.map_to_model` | Build atomic model from a cryo-EM map |
| `phenix.dock_in_map` | Dock a model into a map |
| `phenix.segment_and_split_map` | Segment map into domains |

## Model Building and Manipulation

| Command | Purpose |
|---------|---------|
| `phenix.autobuild` | Automated model building into density |
| `phenix.fit_loops` | Fit or rebuild loop regions |
| `phenix.pdbtools` | PDB manipulation, including selections and B-factor edits |
| `phenix.reduce` | Add hydrogens to a structure |
| `phenix.ready_set` | Add hydrogens and generate ligand restraints |
| `phenix.find_helices_strands` | Identify secondary structure elements |
| `phenix.analyze_alt_conf` | Analyze alternate conformations in a model; can compare against another model |
| `phenix.create_alt_conf` | Generate alternate conformations from a single-conformation starting model and data |
| `phenix.merge_models_as_alt_conf` | Combine several models with identical hierarchies into one multi-conformer model |

### Example: Extract a single chain

```python
run_phenix_tool(
    tool_name="phenix.pdbtools",
    input_files=["multimer.pdb"],
    arguments={"selection": '"chain A"', "output.file_name": "chain_A.pdb"},
    description="Extract chain A from multimer",
)
```

## Ligand Tools

| Command | Purpose |
|---------|---------|
| `phenix.elbow` | Generate ligand geometry and restraints |
| `phenix.ligandfit` | Fit a ligand into electron density |
| `phenix.ligand_identification` | Identify unknown ligand density |
| `phenix.find_all_ligands` | Find all ligand binding sites |

## Data Analysis

| Command | Purpose |
|---------|---------|
| `phenix.xtriage` | Diffraction data analysis |
| `phenix.merging_statistics` | Data merging statistics |
| `phenix.french_wilson` | French-Wilson scaling |
| `phenix.cif_as_mtz` / `phenix.mtz_as_cif` | Reflection file format conversion |
| `phenix.pdb_as_cif` / `phenix.cif_as_pdb` | Model file format conversion |

## Molecular Replacement

| Command | Purpose |
|---------|---------|
| `phenix.phaser` | Molecular replacement |
| `phenix.ensembler` | Prepare search ensembles for molecular replacement |
| `phenix.sculptor` | Edit search models for molecular replacement |
| `phenix.mr_model_preparation` | Prepare molecular replacement search models |

## Sequence and Annotation

| Command | Purpose |
|---------|---------|
| `phenix.print_sequence` | Extract sequence from PDB |
| `phenix.assign_sequence` | Assign a sequence to a model using a map and sequence file |
| `phenix.fetch_pdb` | Download PDB entries by ID |

## Useful Utilities

| Command | Purpose |
|---------|---------|
| `phenix.b_factor_statistics` | B-factor distribution analysis |
| `phenix.find_ncs` | Detect non-crystallographic symmetry |
| `phenix.hbond` | Hydrogen bond analysis |
| `phenix.table_one` | Generate publication-ready Table 1 statistics |

## Tips for Using `run_phenix_tool`

1. Start with `phenix.molprobity` for a quick quality overview.
2. Input files are relative to `data/`.
3. Pass CLI arguments as `arguments={"flag": "value"}`.
4. Avoid large refinement jobs that are likely to hit the 5-minute timeout.
5. Check `phenix.pdbtools` before writing custom PDB manipulation code.
6. Use `phenix.pdb_as_cif` or `phenix.cif_as_pdb` for format conversion.
