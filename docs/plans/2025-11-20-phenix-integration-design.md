# OpenScientist-Phenix Integration Design

**Date:** 2025-11-20
**Author:** Justin Reese
**Status:** Design - Ready for Implementation
**Branch:** phenix-integration

## Overview

This document describes the integration of Phenix (macromolecular structure determination software) into OpenScientist as a proof of concept for autonomous structural biology analysis.

### Problem Statement

Structural biologists often need to validate experimental structures against computational predictions (AlphaFold). While Phenix provides individual tools for comparison and validation, interpreting discrepancies requires:
- Running multiple validation tools
- Cross-referencing different metrics
- Searching literature for similar observations
- Understanding biological context (flexible regions, conformational states, artifacts)

This is time-consuming and requires deep expertise. An autonomous agent can iteratively explore discrepancies, test hypotheses about their causes, and synthesize findings from literature.

### Key Use Case (Proof of Concept)

Given an experimental PDB/mmCIF file and an AlphaFold prediction, OpenScientist-Phenix will:
1. Compare structures and identify regions of disagreement
2. Generate hypotheses about why discrepancies exist
3. Test hypotheses using Phenix validation tools
4. Search literature for similar observations
5. Produce a report explaining findings with mechanistic insights

### Success Criteria

- Agent can align two structures using Phenix
- Agent identifies and explains discrepancies
- Agent searches literature for structural insights
- Agent generates coherent final report
- Runs in ~10 iterations with reasonable findings

## Architecture

### Integration Approach

**Domain extension (not fork)** - OpenScientist-Phenix extends OpenScientist as a new domain, similar to how metabolomics and genomics are handled. This keeps the codebase unified while allowing domain-specific capabilities.

### New Components

#### 1. MCP Tools

**Location:** `src/open_scientist/mcp_server/phenix_tools.py` (new file)

Add three new MCP tools for Phenix operations:

**`run_phenix_tool`** - Execute any Phenix command-line tool
- Input: tool name (e.g., "phenix.clashscore"), PDB/mmCIF file paths, parameters
- Output: Tool output (validation reports, scores, logs)
- Uses subprocess to call Phenix with proper environment setup
- Parses structured output when available

**`compare_structures`** - Specialized wrapper for structure comparison
- Input: experimental PDB path, predicted PDB path
- Internally calls `phenix.superpose_pdbs`
- Returns: RMSD, residue-level differences, alignment info
- Extracts key metrics for the agent to analyze

**`parse_alphafold_confidence`** - Extract pLDDT and PAE data
- Input: AlphaFold PDB file, optional PAE JSON
- Output: Per-residue confidence scores, domain boundaries from PAE
- Helps agent correlate low confidence regions with discrepancies

**Tool Registration:**

Tools are conditionally registered only if `PHENIX_PATH` is configured:

```python
# In src/open_scientist/mcp_server/server.py
phenix_path = os.getenv('PHENIX_PATH')
if phenix_path:
    from . import phenix_tools
    phenix_tools.register_tools(server)
```

**Keep existing tools** - `execute_code`, `search_pubmed`, `update_knowledge_state` remain available and useful for structural biology.

#### 2. Skills

**Location:** `.claude/skills/domain/structural_biology/`

Create domain-specific skills for structural biology workflows:

- `comparing-structures.md` - How to align and compare experimental vs. predicted structures
- `interpreting-discrepancies.md` - Framework for explaining structural differences
- `validation-metrics.md` - Understanding Phenix validation scores (clashscore, Ramachandran, geometry)
- `alphafold-confidence.md` - Interpreting pLDDT, PAE, and confidence metrics
- `phenix-tools-reference.md` - Quick reference for available Phenix tools

These skills provide the agent with structural biology domain knowledge.

#### 3. File Upload Handling

**Location:** `src/open_scientist/web_app.py`

Extend file upload to support PDB/mmCIF formats:

```python
# Allowed file extensions for uploads
# To add new file types, update this set and restart the app
# Note: This is also documented in .env.example
ALLOWED_EXTENSIONS = {'.csv', '.tsv', '.pdb', '.cif', '.ent', '.mmcif'}
```

Update file handling logic:
- Preserve original filenames for structure files
- Store in `jobs/job_<id>/data/`
- Update job metadata to track file types and purposes

#### 4. Environment Configuration

**New environment variable (`.env`):**

```bash
# Phenix installation path (optional, for structural biology features)
# If not set, Phenix tools will be unavailable
PHENIX_PATH=/Applications/phenix-1.21.2-5419

# Note: Allowed file extensions are configured in src/open_scientist/web_app.py
# Currently supports: .csv, .tsv, .pdb, .cif, .ent, .mmcif
```

**Phenix environment setup:**

Create `src/open_scientist/phenix_setup.py`:

```python
import os
import subprocess

def setup_phenix_env():
    """Source Phenix environment and return updated env dict"""
    phenix_path = os.getenv('PHENIX_PATH')
    if not phenix_path:
        return None

    env_script = f"{phenix_path}/phenix_env.sh"
    cmd = f"source {env_script} && env"
    proc = subprocess.run(cmd, shell=True, capture_output=True,
                         text=True, executable='/bin/bash')

    phenix_env = os.environ.copy()
    for line in proc.stdout.split('\n'):
        if '=' in line:
            key, value = line.split('=', 1)
            phenix_env[key] = value

    return phenix_env
```

## Workflow

### Autonomous Discovery Loop

Job runs for N iterations (e.g., 10) with full autonomy. The agent decides what to investigate, when, and how.

**Tools available to agent:**
- `run_phenix_tool` - Execute any Phenix command
- `compare_structures` - Wrapper for structure alignment
- `parse_alphafold_confidence` - Extract pLDDT/PAE data
- `execute_code` - Python analysis and visualization
- `search_pubmed` - Literature search
- `update_knowledge_state` - Record findings

**Skills provide domain knowledge:**
- How to use Phenix validation tools
- How to interpret structural metrics (RMSD, clashscore, etc.)
- How to analyze AlphaFold confidence scores
- How to formulate structural biology hypotheses

**Agent decides autonomously:**
- What to investigate first
- Which tools to use when
- What hypotheses to test
- When to search literature
- How to synthesize findings
- When it has enough information

### Typical Discovery Pattern

While the agent is fully autonomous, a typical workflow might follow this pattern:

**Initial exploration:**
- Align structures with `phenix.superpose_pdbs`
- Calculate RMSD, identify high-deviation regions
- Parse AlphaFold confidence scores
- Record observations

**Hypothesis generation:**
- Correlate discrepancies with confidence/validation metrics
- Generate hypotheses about causes
- Initial literature searches

**Testing and refinement:**
- Run targeted Phenix validation tools
- Custom analyses with Python
- Literature deep-dives on specific findings
- Refine or reject hypotheses

**Synthesis:**
- Connect findings into coherent explanations
- Final report generation

### Job Initialization

1. **User uploads files via web UI:**
   - `experimental.pdb` - Crystal/cryo-EM structure
   - `alphafold.pdb` - AlphaFold prediction
   - Research question: "Compare experimental and AlphaFold structures, explain discrepancies"

2. **Job setup:**
   - Files copied to `jobs/job_<id>/data/`
   - Phenix environment sourced
   - Skills loaded: `structural_biology/*` + generic `workflow/*`
   - Knowledge graph initialized with sections: structures, discrepancies, hypotheses, findings

### Outputs

- **Knowledge graph** (`knowledge_graph.json`): Structured findings
- **Plots**: Structure alignments, RMSD plots, confidence scores
- **Final report**: Markdown summary with key discrepancies explained
- **Tool logs**: All Phenix command outputs for transparency

## Implementation Details

### MCP Tool Implementation

**File structure:**
```
src/open_scientist/mcp_server/
├── __init__.py
├── server.py           # Core tools (execute_code, search_pubmed, etc.)
├── phenix_tools.py     # Phenix-specific tools (new)
```

**Example: `run_phenix_tool` implementation:**

```python
async def run_phenix_tool(tool_name: str, input_files: list[str],
                         arguments: dict = None, description: str = ""):
    """Execute a Phenix command-line tool"""

    # Get Phenix environment
    from open_scientist.phenix_setup import setup_phenix_env
    phenix_env = setup_phenix_env()
    if not phenix_env:
        return [types.TextContent(
            type="text",
            text="Error: PHENIX_PATH not configured"
        )]

    # Build command
    cmd = [tool_name] + input_files
    if arguments:
        for key, val in arguments.items():
            cmd.append(f"{key}={val}")

    # Execute with timeout
    result = subprocess.run(
        cmd,
        env=phenix_env,
        capture_output=True,
        text=True,
        timeout=300  # 5 min timeout
    )

    # Format output
    output = f"=== {description} ===\n\n"
    output += f"Command: {' '.join(cmd)}\n\n"
    output += result.stdout
    if result.stderr:
        output += f"\nErrors:\n{result.stderr}"

    return [types.TextContent(type="text", text=output)]
```

### Skills Content

**Example: `comparing-structures.md`**

```markdown
---
name: comparing-structures
description: How to align and compare experimental vs predicted protein structures
---

# Comparing Protein Structures

When comparing experimental and predicted structures:

## Initial Comparison

Use `compare_structures` tool with experimental and predicted PDB files.
This runs phenix.superpose_pdbs and reports:
- Global RMSD (Å)
- Per-residue deviations
- Alignment quality

## Interpreting RMSD

- RMSD < 1.0 Å: Excellent agreement
- RMSD 1-2 Å: Good agreement, minor differences
- RMSD 2-4 Å: Moderate differences, investigate regions
- RMSD > 4 Å: Significant differences, likely different conformations

## Finding Problematic Regions

After global alignment, identify specific regions with high deviation:
- Look for stretches of high per-residue RMSD
- Cross-reference with AlphaFold confidence (pLDDT)
- Consider secondary structure context

## Common Causes of Discrepancies

- Flexible loops (low pLDDT, high B-factors)
- Conformational changes (ligand binding, activation)
- Domain orientations (hinges, multi-domain proteins)
- Crystal contacts or artifacts
- Missing density in experimental structure
```

## Edge Cases

### 1. Phenix Not Available
- **Detection:** Check `PHENIX_PATH` on startup
- **Behavior:** Skip registering Phenix tools, show warning in UI
- **User message:** "Structural biology features require Phenix installation. Set PHENIX_PATH in .env to enable."

### 2. Structure Alignment Fails
- **Causes:** Different sequences, truncated structures, file corruption
- **Handling:** `compare_structures` catches errors and returns informative message
- **Agent action:** Try alternative approaches (different chains, sequence alignment first)

### 3. Phenix Tool Timeout
- **Default timeout:** 5 minutes per tool
- **Handling:** Catch timeout exception, log it, continue job
- **Agent action:** Learn tool failed and try different approach

### 4. No AlphaFold Prediction Provided
- **Proof of concept:** Require user to upload both files (simpler)
- **Future:** Agent could fetch it using UniProt ID from PDB header

### 5. File Format Issues
- **PDB vs mmCIF:** Phenix handles both
- **Chain naming:** Multi-chain structures (e.g., tetramers) - agent decides which to compare
- **Missing atoms:** Experimental structures often have missing residues
- **Handling:** Phenix tools report these; agent interprets them

### 6. Literature Search Yields No Results
- **Common:** Very specific residue-level queries might have no matches
- **Agent behavior:** Broaden search terms, look for protein family, general features
- **Skills guide:** How to formulate effective PubMed queries

## Testing Strategy

### Test Case: TP53 (p53 tumor suppressor)

**Files:** `test_data/tp53/`
- `3ts8.pdb` - Experimental crystal structure (multidomain tetramer bound to DNA)
- `alphafold_P04637.pdb` - AlphaFold prediction (393 residues, pLDDT 75.06)
- `alphafold_P04637_pae.json` - Predicted aligned error matrix

**Why TP53:**
- Well-studied protein with rich literature
- Known to have multiple conformations
- DNA-binding with conformational changes
- Flexible regions with varying AlphaFold confidence
- Good test of literature search capabilities

### Manual Test (Phase 1)

1. Upload both files via web UI
2. Research question: "Compare experimental and AlphaFold p53 structures, explain discrepancies"
3. Set iterations: 10
4. Enable skills: structural_biology
5. Verify:
   - Phenix tools execute successfully
   - RMSD calculated correctly
   - Literature searches occur
   - Knowledge graph populated
   - Final report generated

### Automated Test (Phase 2)

Create `tests/test_phenix_integration.py`:
- Test MCP tools in isolation
- Test environment setup
- Test file handling
- Mock Phenix calls for CI/CD

## Migration Path

### Current OpenScientist Users
- **No impact** - Phenix tools only load if `PHENIX_PATH` set
- Existing metabolomics/genomics jobs work unchanged
- Can add structural biology by setting environment variable

### Structural Biology Users
- Set `PHENIX_PATH` in `.env`
- Upload PDB files instead of CSV
- Select structural biology domain (or auto-detect)
- Same workflow: research question, iterations, skills

### Shared Infrastructure
- Knowledge graph works for all domains
- Literature search works for all domains
- Job management, web UI, cost tracking unchanged
- Skills system supports all domains

## Future Extensions

### Phase 2: Density Map Support
- Accept MRC/CCP4 files (cryo-EM density)
- Accept MTZ files (X-ray diffraction data)
- Tools: `phenix.real_space_refine`, `phenix.refine`
- Use case: "Refine my model against this density map"

### Phase 3: Iterative Refinement
- Agent improves structures, not just analyzes
- Run refinement cycles with different parameters
- Test hypotheses by modifying and re-refining
- Use case: "Optimize my structure refinement"

### Phase 4: Multi-Model Comparison
- Compare multiple experimental structures (NMR ensembles)
- Compare across homologs
- Batch processing of structure sets

### Phase 5: Visualization
- Generate PyMOL scripts
- Interactive 3D viewer in web UI (NGL viewer, Mol*)
- Annotate structures with findings

### Phase 6: Other Prediction Tools
- RoseTTAFold, ESMFold comparison
- Ensemble predictions (AlphaFold multimer)
- Multiple sequence alignment influence

### Phase 7: Domain Expansion
- Support other packages (CCP4, Rosetta)
- MD simulation analysis
- Docking and binding site analysis

## Summary

### What We're Building

**Minimal viable integration (proof of concept):**

1. **MCP tools:** `run_phenix_tool`, `compare_structures`, `parse_alphafold_confidence`
2. **Skills:** 4-5 structural biology skills in `.claude/skills/domain/structural_biology/`
3. **File support:** PDB/mmCIF upload and handling
4. **Environment:** Phenix setup via `PHENIX_PATH`
5. **Test case:** TP53 experimental vs AlphaFold comparison
6. **Use case:** Autonomous discrepancy analysis with literature-grounded explanations

**Not included in proof of concept:**
- Density maps or refinement
- Multi-structure comparison
- Automated AlphaFold fetching
- Visualization beyond matplotlib plots
- Other structural biology packages

### Implementation Priority

1. Environment setup and Phenix tool execution
2. Structure comparison MCP tool
3. Basic skills for structural biology
4. File upload handling for PDB/mmCIF
5. Test with TP53 case
6. Iterate based on findings
