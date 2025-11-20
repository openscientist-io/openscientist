# SHANDY-Phenix Quick Start Guide

**You can now use SHANDY for structural biology analysis!**

## What's New

SHANDY now supports:
- ✅ **PDB/mmCIF file uploads** - Upload experimental and predicted protein structures
- ✅ **Phenix integration** - Run Phenix tools for structure comparison and validation
- ✅ **AlphaFold analysis** - Extract and interpret confidence scores (pLDDT, PAE)
- ✅ **Structural biology skills** - Domain-specific knowledge for autonomous discovery

## Prerequisites

1. **Phenix installed** - Already configured at `/Applications/phenix-1.21.2-5419/`
2. **PHENIX_PATH set** - Already added to `.env`
3. **SHANDY running** - Start the web app (see below)

## Quick Test with TP53 Data

### Step 1: Start SHANDY

```bash
# From shandy directory
python -m shandy.web_app
```

The web interface will be available at `http://localhost:8080`

### Step 2: Upload Test Files

Test data is ready in `test_data/tp53/`:
- `3ts8.pdb` - Experimental crystal structure (p53 tetramer bound to DNA)
- `alphafold_P04637.pdb` - AlphaFold prediction (full-length p53)

### Step 3: Submit Job

In the web UI:

1. **Research Question:**
   ```
   Compare experimental p53 structure (3ts8.pdb) with AlphaFold prediction (alphafold_P04637.pdb).
   Identify and explain structural discrepancies using Phenix tools and literature search.
   Focus on understanding why specific regions differ and what this reveals about p53 biology.
   ```

2. **Upload Files:**
   - Click "Upload Data Files"
   - Select `test_data/tp53/3ts8.pdb`
   - Select `test_data/tp53/alphafold_P04637.pdb`

3. **Configuration:**
   - Max Iterations: `10`
   - Use Skills: ✅ (enabled)

4. **Click "Start Discovery"**

### Step 4: Monitor Progress

- Navigate to "View Jobs" to see your job running
- Click on the job ID to see detailed progress
- Watch as the agent:
  - Compares structures with Phenix
  - Identifies high-deviation regions
  - Checks AlphaFold confidence scores
  - Searches literature for explanations
  - Generates hypotheses and tests them
  - Creates plots and visualizations
  - Produces final report with findings

### Step 5: Review Results

After 10 iterations (should complete in ~10-20 minutes), check:

**Knowledge Graph** - Structured findings with:
- Structure comparison results
- Identified discrepancies
- Hypotheses tested
- Literature references
- Confirmed findings

**Plots** - Visual analyses:
- RMSD comparisons
- Confidence score profiles
- Per-residue deviation plots

**Final Report** - Summary of:
- Why experimental and AlphaFold structures differ
- Which regions show discrepancies
- Biological explanations from literature
- Functional implications

## Available Tools

The agent has access to these new Phenix tools:

### `run_phenix_tool`
Execute any Phenix command:
```python
run_phenix_tool(
    tool_name="phenix.clashscore",
    input_files=["3ts8.pdb"],
    description="Checking for steric clashes"
)
```

### `compare_structures`
Align and compare two structures:
```python
compare_structures(
    experimental_pdb="3ts8.pdb",
    predicted_pdb="alphafold_P04637.pdb",
    description="Initial structure comparison"
)
```

### `parse_alphafold_confidence`
Extract pLDDT scores:
```python
parse_alphafold_confidence(
    alphafold_pdb="alphafold_P04637.pdb",
    pae_json="alphafold_P04637_pae.json"  # optional
)
```

Plus all existing tools:
- `execute_code` - Python analysis with Biopython
- `search_pubmed` - Literature search
- `update_knowledge_graph` - Record findings

## Skills Loaded

When running structural biology jobs, these skills guide the agent:

- **comparing-structures.md** - How to align and interpret RMSD
- **alphafold-confidence.md** - Understanding pLDDT and PAE metrics
- **validation-metrics.md** - Using Phenix validation tools
- **interpreting-discrepancies.md** - Framework for explaining differences

## Expected Findings

Based on the literature, you should see:

1. **Moderate RMSD** (~2-4 Å) due to:
   - 3ts8 is DNA-bound (tetramer)
   - AlphaFold predicts apo state (monomer)
   - Conformational changes upon DNA binding

2. **Flexible regions** with:
   - Low AlphaFold confidence (pLDDT < 70)
   - High RMSD
   - Known from literature as functionally important

3. **Literature connections**:
   - Papers on p53 flexibility
   - DNA-binding mechanisms
   - Conformational changes

4. **Biological insights**:
   - Why certain regions are flexible
   - How DNA binding affects structure
   - Functional implications

## Troubleshooting

**"PHENIX_PATH not configured"** error:
- Check `.env` has `PHENIX_PATH=/Applications/phenix-1.21.2-5419`
- Restart the web app

**"Phenix tools not available"** in logs:
- Check stderr output when starting web app
- Should see "✅ Phenix tools registered"
- If not, verify Phenix installation path

**File upload fails:**
- Make sure files are `.pdb` or `.cif` format
- Check file size (should be < 10MB typically)

**Job fails immediately:**
- Check job logs in `jobs/job_<id>/`
- Verify both files were uploaded
- Check research question is clear

## Next Steps

After testing with TP53:

1. **Try your own structures:**
   - Upload experimental PDB from RCSB PDB
   - Download AlphaFold prediction from AlphaFold DB
   - Compare and analyze

2. **Explore different proteins:**
   - Proteins with known conformational changes
   - Multi-domain proteins
   - Membrane proteins

3. **Advanced analyses:**
   - Add density maps (future feature)
   - Compare multiple structures
   - Validate refinement strategies

## Documentation

- **Design doc:** `docs/plans/2025-11-20-phenix-integration-design.md`
- **Skills:** `.claude/skills/domain/structural_biology/`
- **Test data:** `test_data/tp53/README.md`
- **Manual experiments:** `test_data/tp53/manual_test_experiment.md`

## Getting Help

If you encounter issues:
1. Check the job logs in `jobs/job_<id>/orchestrator.log`
2. Review the knowledge graph in `jobs/job_<id>/knowledge_graph.json`
3. Look at the design document for expected behavior
4. Check Phenix installation with: `source /Applications/phenix-1.21.2-5419/phenix_env.sh && phenix.python --version`

---

**Ready to discover new structural biology insights with autonomous AI!** 🧬🤖
