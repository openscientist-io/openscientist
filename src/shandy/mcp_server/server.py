"""
MCP server implementation for SHANDY tools using FastMCP.

Provides tools for autonomous discovery:
- execute_code: Run Python analysis on data
- search_pubmed: Search scientific literature
- update_knowledge_graph: Record findings
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
from ..knowledge_graph import KnowledgeGraph
from ..literature import search_pubmed as search_pm
from ..phenix_setup import check_phenix_available

# Global state (initialized by main())
DATA: Optional[pd.DataFrame] = None  # None for non-tabular files
DATA_FILES: List[Dict[str, Any]] = []  # Metadata for all data files
DATA_LOAD_ERROR: Optional[str] = None  # Error message if data failed to load
JOB_DIR: Path = None
KG: KnowledgeGraph = None

# Create FastMCP server
mcp = FastMCP("shandy-tools")


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
    global DATA, DATA_FILES, DATA_LOAD_ERROR, JOB_DIR, KG

    # Check if data failed to load during startup
    if DATA_LOAD_ERROR is not None:
        return f"❌ ERROR: Cannot execute code - data file failed to load.\n\n{DATA_LOAD_ERROR}"

    # Reload knowledge graph to get latest state (orchestrator may have incremented iteration)
    KG = KnowledgeGraph.load(JOB_DIR / "knowledge_graph.json")

    # Create plots directory
    plots_dir = JOB_DIR / "plots"
    plots_dir.mkdir(parents=True, exist_ok=True)

    # Execute code (DATA may be None for non-tabular files)
    result = exec_code(code, DATA, plots_dir, timeout=60, description=description,
                      iteration=KG.data["iteration"], data_files=DATA_FILES)

    # Log to knowledge graph
    KG.log_analysis(
        action="execute_code",
        code=code,
        output=result.get("output", ""),
        success=result["success"],
        execution_time=result["execution_time"],
        plots=result.get("plots", []),
    )
    KG.save(JOB_DIR / "knowledge_graph.json")

    # Format result for Claude
    return format_execution_result(result)


@mcp.tool()
def search_pubmed(query: str, max_results: int = 10) -> str:
    """
    Search PubMed for scientific papers.

    Args:
        query: Search query (e.g., 'hypothermia neuroprotection metabolomics')
        max_results: Maximum number of results to return (default: 10)

    Returns:
        Formatted list of papers with titles, abstracts, and PMIDs
    """
    global JOB_DIR, KG

    # Reload knowledge graph to get latest state (orchestrator may have incremented iteration)
    KG = KnowledgeGraph.load(JOB_DIR / "knowledge_graph.json")

    # Search PubMed
    papers = search_pm(query, max_results=max_results)

    # Log to knowledge graph
    for paper in papers:
        KG.add_literature(
            pmid=paper["pmid"],
            title=paper["title"],
            abstract=paper["abstract"],
            search_query=query,
        )

    KG.log_analysis(action="search_pubmed", query=query, results_count=len(papers))
    KG.save(JOB_DIR / "knowledge_graph.json")

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
def update_knowledge_graph(title: str, evidence: str, interpretation: str = "") -> str:
    """
    Record a confirmed finding to the knowledge graph.

    Args:
        title: Finding title (concise description)
        evidence: Statistical evidence (p-values, effect sizes, etc.)
        interpretation: Biological/mechanistic interpretation (optional)

    Returns:
        Confirmation message with finding ID
    """
    global JOB_DIR, KG

    # Reload knowledge graph to get latest state (orchestrator may have incremented iteration)
    KG = KnowledgeGraph.load(JOB_DIR / "knowledge_graph.json")

    # Add finding
    finding_id = KG.add_finding(title=title, evidence=evidence)

    # Update interpretation if provided
    if interpretation:
        for finding in KG.data["findings"]:
            if finding["id"] == finding_id:
                finding["biological_interpretation"] = interpretation

    KG.save(JOB_DIR / "knowledge_graph.json")

    return f"✅ Finding recorded as {finding_id}: {title}"


def main():
    """Main entry point for MCP server."""
    import argparse
    import time

    parser = argparse.ArgumentParser(description="SHANDY MCP Server")
    parser.add_argument("--job-dir", required=True, help="Job directory")
    parser.add_argument("--data-file", required=True, help="Primary data file")

    args = parser.parse_args()

    # Initialize global state
    global DATA, DATA_FILES, DATA_LOAD_ERROR, JOB_DIR, KG

    JOB_DIR = Path(args.job_dir)
    DATA_LOAD_ERROR = None  # Will be set if loading fails

    # Load primary data file using new loader
    primary_file = Path(args.data_file)

    # Get metadata first (fast operation)
    try:
        primary_info = get_file_info(primary_file)
        DATA_FILES = [primary_info]
    except Exception as e:
        print(f"❌ ERROR: Could not read file info for {primary_file}: {e}", file=sys.stderr)
        sys.exit(1)

    # Log file size to help diagnose loading issues
    file_size_mb = primary_info['size'] / (1024 * 1024)
    print(f"📂 Loading data file: {primary_file.name} ({file_size_mb:.1f} MB)", file=sys.stderr)

    # Load data with error handling
    # IMPORTANT: Don't exit on load failure - let the server start so the agent
    # can see and communicate the error to the user
    start_time = time.time()
    try:
        DATA = load_data_file(primary_file)  # May be None for non-tabular files
        load_time = time.time() - start_time

        if load_time > 10:
            print(f"⏱️  Large file loaded in {load_time:.1f}s", file=sys.stderr)

    except FileTooBigError as e:
        DATA_LOAD_ERROR = f"Unable to load data file: file exceeds size limit ({file_size_mb:.1f} MB). Please contact the administrator."
        DATA = None
        print(f"❌ ERROR: File too large - {file_size_mb:.1f} MB exceeds 100 MB limit", file=sys.stderr)
        print(f"   {type(e).__name__}: {e}", file=sys.stderr)
    except Exception as e:
        DATA_LOAD_ERROR = f"Unable to load data file '{primary_file.name}'. Please contact the administrator."
        DATA = None
        print(f"❌ ERROR: Failed to load {primary_file.name}: {type(e).__name__}: {e}", file=sys.stderr)

    # Scan job data directory for additional files
    data_dir = JOB_DIR / "data"
    if data_dir.exists():
        for file_path in data_dir.iterdir():
            if file_path.is_file() and file_path != primary_file:
                try:
                    file_info = get_file_info(file_path)
                    DATA_FILES.append(file_info)
                except Exception as e:
                    print(f"Warning: Could not process {file_path}: {e}", file=sys.stderr)

    # Log what was loaded
    if DATA is not None:
        print(f"✅ Loaded tabular data from {primary_file.name}: {DATA.shape[0]} rows × {DATA.shape[1]} columns", file=sys.stderr)
    else:
        print(f"ℹ️  Non-tabular file: {primary_file.name} ({primary_info['file_type']})", file=sys.stderr)

    print(f"📁 {len(DATA_FILES)} data file(s) available: {', '.join(f['name'] for f in DATA_FILES)}", file=sys.stderr)

    # Load or create knowledge graph
    kg_path = JOB_DIR / "knowledge_graph.json"
    if kg_path.exists():
        KG = KnowledgeGraph.load(kg_path)
    else:
        raise FileNotFoundError(f"Knowledge graph not found at {kg_path}")

    # Register Phenix tools if available
    if check_phenix_available():
        from . import phenix_tools
        phenix_tools.register_phenix_tools(mcp, JOB_DIR, KG)
        print("✅ Phenix tools registered", file=sys.stderr)
    else:
        print("⚠️  Phenix tools not available (PHENIX_PATH not set)", file=sys.stderr)

    # Run the FastMCP server
    mcp.run()


if __name__ == "__main__":
    main()
