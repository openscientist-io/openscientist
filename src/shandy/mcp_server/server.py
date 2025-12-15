"""
MCP server implementation for SHANDY tools using FastMCP.

Provides tools for autonomous discovery:
- execute_code: Run Python analysis on data
- search_pubmed: Search scientific literature
- update_knowledge_state: Record findings
- run_phenix_tool: Execute Phenix structural biology tools (if available)
- compare_structures: Compare experimental and predicted structures
- parse_alphafold_confidence: Extract AlphaFold confidence metrics
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from mcp.server.fastmcp import FastMCP

# Import tool implementations
from ..code_executor import execute_code as exec_code, format_execution_result
from ..file_loader import load_data_file, get_file_info, FileTooBigError
from ..knowledge_state import KnowledgeState
from ..literature import search_pubmed as search_pm
from ..phenix_setup import check_phenix_available

# Global state (initialized by main())
DATA: Optional[pd.DataFrame] = None  # None for non-tabular files or not yet loaded
DATA_FILES: List[Dict[str, Any]] = []  # Metadata for all data files
DATA_LOAD_ERROR: Optional[str] = None  # Error message if data failed to load
DATA_FILE_PATH: Optional[Path] = None  # Path to primary data file (for lazy loading)
DATA_LOADED: bool = False  # Track whether we've attempted to load data
JOB_DIR: Path = None
KS: KnowledgeState = None

# Create FastMCP server
mcp = FastMCP("shandy-tools")


def ensure_data_loaded() -> Optional[str]:
    """
    Ensure data is loaded before executing code.

    Returns error message if loading failed, None if successful.
    """
    global DATA, DATA_LOADED, DATA_LOAD_ERROR, DATA_FILE_PATH

    # Already attempted to load?
    if DATA_LOADED:
        return DATA_LOAD_ERROR

    # Mark as loaded (even if it fails, don't retry every time)
    DATA_LOADED = True

    # No file path means no data was provided (valid case)
    if DATA_FILE_PATH is None:
        DATA_LOAD_ERROR = None  # Not an error, just no data
        DATA = None
        return None  # Success - no data to load

    # Load data now
    file_size_mb = DATA_FILE_PATH.stat().st_size / (1024 * 1024)
    print(f"⏳ Loading data file on first use: {DATA_FILE_PATH.name} ({file_size_mb:.1f} MB)", file=sys.stderr)

    import time
    start_time = time.time()

    try:
        DATA = load_data_file(DATA_FILE_PATH)
        load_time = time.time() - start_time

        if DATA is not None:
            print(f"✅ Loaded tabular data: {DATA.shape[0]} rows × {DATA.shape[1]} columns in {load_time:.1f}s", file=sys.stderr)
        else:
            print(f"ℹ️  Non-tabular file loaded in {load_time:.1f}s", file=sys.stderr)

        return None  # Success

    except FileTooBigError as e:
        DATA_LOAD_ERROR = f"Unable to load data file: file exceeds size limit ({file_size_mb:.1f} MB). Please contact the administrator."
        print(f"❌ {DATA_LOAD_ERROR}", file=sys.stderr)
        return DATA_LOAD_ERROR

    except Exception as e:
        DATA_LOAD_ERROR = f"Unable to load data file '{DATA_FILE_PATH.name}': {type(e).__name__}: {e}"
        print(f"❌ {DATA_LOAD_ERROR}", file=sys.stderr)
        return DATA_LOAD_ERROR


@mcp.tool()
def execute_code(code: str, description: str = "") -> str:
    """
    Execute Python code to analyze data.

    The code has access to:
    - 'data' variable: DataFrame with tabular data (if available, otherwise None)
    - 'data_files' variable: List of file metadata dicts with paths, types, etc.

    For structure files (PDB, CIF) or other non-tabular data, use the file paths
    from data_files to load and analyze them directly.

    Plots are automatically saved to the job's plots directory.

    Args:
        code: Python code to execute
        description: Optional description of what you're investigating (e.g., "Testing hypothesis about gene upregulation in condition X")

    Returns:
        Formatted execution result with output, plots, and any errors
    """
    global DATA, DATA_FILES, JOB_DIR, KS

    # Lazy load data on first execute_code call
    load_error = ensure_data_loaded()
    if load_error is not None:
        return f"❌ ERROR: Cannot execute code - data file failed to load.\n\n{load_error}"

    # Reload knowledge graph to get latest state (orchestrator may have incremented iteration)
    KS = KnowledgeState.load(JOB_DIR / "knowledge_state.json")

    # Create provenance directory for plots and artifacts
    provenance_dir = JOB_DIR / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)

    # Execute code (DATA may be None for non-tabular files)
    result = exec_code(code, DATA, provenance_dir, timeout=60, description=description,
                      iteration=KS.data["iteration"], data_files=DATA_FILES)

    # Log to knowledge graph (code is stored in plot metadata files, not here)
    KS.log_analysis(
        action="execute_code",
        description=description,
        output=result.get("output", ""),
        success=result["success"],
        execution_time=result["execution_time"],
        plots=result.get("plots", []),
    )
    KS.save(JOB_DIR / "knowledge_state.json")

    # Format result for Claude
    return format_execution_result(result)


@mcp.tool()
def search_pubmed(query: str, max_results: int = 10, description: str = "") -> str:
    """
    Search PubMed for scientific papers.

    Args:
        query: Search query (e.g., 'hypothermia neuroprotection metabolomics')
        max_results: Maximum number of results to return (default: 10)
        description: Why you're searching (e.g., "Looking for prior work on carnosine and oxidative stress")

    Returns:
        Formatted list of papers with titles, abstracts, and PMIDs
    """
    global JOB_DIR, KS

    # Reload knowledge graph to get latest state (orchestrator may have incremented iteration)
    KS = KnowledgeState.load(JOB_DIR / "knowledge_state.json")

    # Search PubMed
    papers = search_pm(query, max_results=max_results)

    # Log to knowledge graph
    for paper in papers:
        KS.add_literature(
            pmid=paper["pmid"],
            title=paper["title"],
            abstract=paper["abstract"],
            search_query=query,
        )

    KS.log_analysis(action="search_pubmed", query=query, results_count=len(papers), description=description)
    KS.save(JOB_DIR / "knowledge_state.json")

    # Format results
    if not papers:
        return f"No papers found for query: '{query}'"

    result_parts = [f"Found {len(papers)} papers for query: '{query}'\n"]
    for i, paper in enumerate(papers, 1):
        result_parts.append(
            f"\n{i}. **{paper['title']}** (PMID: {paper['pmid']}, {paper.get('year', 'N/A')})\n"
            f"   Authors: {paper.get('authors', 'Unknown')}\n"
            f"   Abstract: {paper['abstract'][:300]}...\n"
        )
    return "".join(result_parts)


@mcp.tool()
def update_knowledge_state(title: str, evidence: str, interpretation: str = "", description: str = "") -> str:
    """
    Record a confirmed finding to the knowledge graph.

    Args:
        title: Finding title (concise description)
        evidence: Statistical evidence (p-values, effect sizes, etc.)
        interpretation: Biological/mechanistic interpretation (optional)
        description: Why you're recording this finding (e.g., "This correlation confirms our hypothesis")

    Returns:
        Confirmation message with finding ID
    """
    global JOB_DIR, KS

    # Reload knowledge graph to get latest state (orchestrator may have incremented iteration)
    KS = KnowledgeState.load(JOB_DIR / "knowledge_state.json")

    # Add finding
    finding_id = KS.add_finding(title=title, evidence=evidence)

    # Update interpretation if provided
    if interpretation:
        for finding in KS.data["findings"]:
            if finding["id"] == finding_id:
                finding["biological_interpretation"] = interpretation

    # Log the action
    KS.log_analysis(action="update_knowledge_state", finding_id=finding_id, title=title, description=description)
    KS.save(JOB_DIR / "knowledge_state.json")

    return f"✅ Finding recorded as {finding_id}: {title}"


@mcp.tool()
def save_iteration_summary(summary: str, strapline: str = "") -> str:
    """
    Save a plain-language summary of what was accomplished this iteration.

    IMPORTANT: Call this as your FINAL action before the iteration ends.
    Do NOT call this until you have completed all investigation work for
    this iteration. The summary should reflect what you actually did and
    discovered, not what you plan to do.

    Args:
        summary: Plain-language summary (1-2 sentences) of what you investigated
                 and what you learned. Do NOT include "Iteration X:" prefix -
                 the system adds that automatically.

                 Good example: "Analyzed correlation between gene expression
                 and treatment groups. Found significant upregulation of
                 stress response genes in treated samples."

                 Bad example: "Iteration 3: Analyzed correlation..." (don't do this)
                 Bad example: "Planning to investigate X..." (summarize what you DID, not what you plan)

        strapline: Short, punchy title summarizing this iteration (5-10 words).
                   Should reflect what was accomplished.
                   Example: "Found stress gene upregulation in treatment"
                   Example: "Ruled out oxidative stress hypothesis"

    Returns:
        Confirmation message
    """
    global JOB_DIR, KS

    # Reload knowledge graph to get latest state
    KS = KnowledgeState.load(JOB_DIR / "knowledge_state.json")

    # Get current iteration
    current_iteration = KS.data["iteration"]

    # Save the summary with strapline
    KS.add_iteration_summary(current_iteration, summary, strapline=strapline)
    KS.save(JOB_DIR / "knowledge_state.json")

    return f"✅ Summary saved for iteration {current_iteration}"


def main():
    """Main entry point for MCP server."""
    import argparse

    parser = argparse.ArgumentParser(description="SHANDY MCP Server")
    parser.add_argument("--job-dir", required=True, help="Job directory")
    parser.add_argument("--data-file", required=False, default=None, help="Primary data file (optional)")

    args = parser.parse_args()

    # Initialize global state
    global DATA_FILES, DATA_FILE_PATH, JOB_DIR, KS

    JOB_DIR = Path(args.job_dir)

    # Save primary data file path for lazy loading (if provided)
    if args.data_file:
        primary_file = Path(args.data_file)
        DATA_FILE_PATH = primary_file

        # Get metadata only (fast operation - no actual data loading)
        try:
            primary_info = get_file_info(primary_file)
            DATA_FILES = [primary_info]
        except Exception as e:
            print(f"❌ ERROR: Could not read file info for {primary_file}: {e}", file=sys.stderr)
            sys.exit(1)

        # Log file info (but don't load data yet)
        file_size_mb = primary_info['size'] / (1024 * 1024)
        print(f"📂 Data file registered: {primary_file.name} ({file_size_mb:.1f} MB) - will load on first use", file=sys.stderr)
    else:
        # No primary data file
        DATA_FILE_PATH = None
        DATA_FILES = []
        print(f"ℹ️  No data file provided - server running in no-data mode", file=sys.stderr)

    # Scan job data directory for additional files (metadata only)
    data_dir = JOB_DIR / "data"
    if data_dir.exists():
        for file_path in data_dir.iterdir():
            if file_path.is_file() and file_path != primary_file:
                try:
                    file_info = get_file_info(file_path)
                    DATA_FILES.append(file_info)
                except Exception as e:
                    print(f"Warning: Could not process {file_path}: {e}", file=sys.stderr)

    print(f"📁 {len(DATA_FILES)} data file(s) available: {', '.join(f['name'] for f in DATA_FILES)}", file=sys.stderr)

    # Load or create knowledge graph
    ks_path = JOB_DIR / "knowledge_state.json"
    if ks_path.exists():
        KS = KnowledgeState.load(ks_path)
    else:
        raise FileNotFoundError(f"Knowledge graph not found at {ks_path}")

    # Register Phenix tools if available
    if check_phenix_available():
        from . import phenix_tools
        phenix_tools.register_phenix_tools(mcp, JOB_DIR, KS)
        print("✅ Phenix tools registered", file=sys.stderr)
    else:
        print("⚠️  Phenix tools not available (PHENIX_PATH not set)", file=sys.stderr)

    # Run the FastMCP server
    mcp.run()


if __name__ == "__main__":
    main()
