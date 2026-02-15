"""
MCP server implementation for SHANDY tools using FastMCP.

Provides tools for autonomous discovery:
- execute_code: Run Python analysis on data
- search_pubmed: Search scientific literature
- add_hypothesis: Record a hypothesis to test
- update_hypothesis: Update hypothesis status with test results
- update_knowledge_state: Record findings
- run_phenix_tool: Execute Phenix structural biology tools (if available)
- compare_structures: Compare experimental and predicted structures
- parse_alphafold_confidence: Extract AlphaFold confidence metrics
"""

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import UUID

import pandas as pd
from mcp.server.fastmcp import FastMCP

# Import tool implementations
from shandy.code_executor import execute_code as exec_code
from shandy.code_executor import format_execution_result
from shandy.document_reader import read_document as read_doc
from shandy.exceptions import FileLoadError
from shandy.file_loader import (
    FileTooBigError,
    get_file_info,
    load_data_file,
)
from shandy.knowledge_state import KnowledgeState
from shandy.literature import search_pubmed as search_pm
from shandy.phenix_setup import check_phenix_available
from shandy.settings import get_settings

# Global state (initialized by main())
DATA: Optional[pd.DataFrame] = None  # None for non-tabular files or not yet loaded
DATA_FILES: List[Dict[str, Any]] = []  # Metadata for all data files
DATA_LOAD_ERROR: Optional[str] = None  # Error message if data failed to load
DATA_FILE_PATH: Optional[Path] = None  # Path to primary data file (for lazy loading)
DATA_LOADED: bool = False  # Track whether we've attempted to load data
JOB_DIR: Optional[Path] = None
KS: Optional[KnowledgeState] = None

# Create FastMCP server
mcp = FastMCP("shandy-tools")


def ensure_data_loaded() -> Optional[str]:
    """
    Ensure data is loaded before executing code.

    Returns error message if loading failed, None if successful.
    """
    global DATA, DATA_LOADED, DATA_LOAD_ERROR

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
    print(
        f"⏳ Loading data file on first use: {DATA_FILE_PATH.name} ({file_size_mb:.1f} MB)",
        file=sys.stderr,
    )

    start_time = time.time()

    try:
        DATA = load_data_file(DATA_FILE_PATH)
        load_time = time.time() - start_time

        if DATA is not None:
            print(
                f"✅ Loaded tabular data: {DATA.shape[0]} rows × {DATA.shape[1]} columns in {load_time:.1f}s",
                file=sys.stderr,
            )
        else:
            print(f"ℹ️  Non-tabular file loaded in {load_time:.1f}s", file=sys.stderr)

        return None  # Success

    except FileTooBigError:
        DATA_LOAD_ERROR = f"Unable to load data file: file exceeds size limit ({file_size_mb:.1f} MB). Please contact the administrator."
        print(f"❌ {DATA_LOAD_ERROR}", file=sys.stderr)
        return DATA_LOAD_ERROR

    except (ValueError, OSError, FileLoadError) as e:
        DATA_LOAD_ERROR = (
            f"Unable to load data file '{DATA_FILE_PATH.name}': {type(e).__name__}: {e}"
        )
        print(f"❌ {DATA_LOAD_ERROR}", file=sys.stderr)
        return DATA_LOAD_ERROR


def _execute_in_container(
    code: str,
    description: str,
    provenance_dir: Path,
    iteration: int,
) -> Dict[str, Any]:
    """
    Execute code in an isolated Docker container.

    Args:
        code: Python code to execute
        description: Description of what's being investigated
        provenance_dir: Directory to save plots and artifacts
        iteration: Current iteration number

    Returns:
        Execution result dictionary
    """
    from shandy.container_manager import get_container_manager

    assert JOB_DIR is not None, "JOB_DIR not initialized"

    # Get job ID from directory name
    job_id = JOB_DIR.name

    # Get data file path if available
    data_path = str(DATA_FILE_PATH) if DATA_FILE_PATH else None

    # Execute in container
    container_manager = get_container_manager()
    result = container_manager.execute_code(
        code=code,
        job_id=job_id,
        data_path=data_path,
        output_dir=provenance_dir,
        timeout=60,
        description=description,
        iteration=iteration,
        data_files=DATA_FILES,
    )

    return result


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
    global KS
    assert JOB_DIR is not None, "JOB_DIR not initialized"

    # Lazy load data on first execute_code call
    load_error = ensure_data_loaded()
    if load_error is not None:
        return f"❌ ERROR: Cannot execute code - data file failed to load.\n\n{load_error}"

    # Reload knowledge graph to get latest state (orchestrator may have incremented iteration)
    KS = KnowledgeState.load(JOB_DIR / "knowledge_state.json")

    # Create provenance directory for plots and artifacts
    provenance_dir = JOB_DIR / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)

    # Execute code - use container isolation if enabled
    if get_settings().container.use_container_isolation:
        result = _execute_in_container(
            code=code,
            description=description,
            provenance_dir=provenance_dir,
            iteration=int(KS.data["iteration"]),
        )
    else:
        # Execute code in-process (DATA may be None for non-tabular files)
        result = exec_code(
            code,
            DATA,
            provenance_dir,
            timeout=60,
            description=description,
            iteration=int(KS.data["iteration"]),
            data_files=DATA_FILES,
        )

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
    global KS
    assert JOB_DIR is not None, "JOB_DIR not initialized"

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

    KS.log_analysis(
        action="search_pubmed",
        query=query,
        results_count=len(papers),
        description=description,
    )
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
def update_knowledge_state(
    title: str, evidence: str, interpretation: str = "", description: str = ""
) -> str:
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
    global KS
    assert JOB_DIR is not None, "JOB_DIR not initialized"

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
    KS.log_analysis(
        action="update_knowledge_state",
        finding_id=finding_id,
        title=title,
        description=description,
    )
    KS.save(JOB_DIR / "knowledge_state.json")

    return f"✅ Finding recorded as {finding_id}: {title}"


@mcp.tool()
def add_hypothesis(statement: str) -> str:
    """
    Record a new hypothesis to test.

    Use this to formally track hypotheses before testing them. This creates
    a structured record that links your hypothesis to subsequent tests and
    findings.

    Workflow:
    1. add_hypothesis("X causes Y") → returns H001
    2. Test the hypothesis with execute_code
    3. update_hypothesis(H001, status="supported" or "rejected", ...)
    4. If supported, optionally record as finding with update_knowledge_state

    Args:
        statement: Clear, testable hypothesis statement
                   (e.g., "Carnosine levels are elevated in hypothermic samples")

    Returns:
        Confirmation with hypothesis ID (e.g., "H001")
    """
    global KS
    assert JOB_DIR is not None, "JOB_DIR not initialized"

    # Reload knowledge graph to get latest state
    KS = KnowledgeState.load(JOB_DIR / "knowledge_state.json")

    # Add hypothesis
    hypothesis_id = KS.add_hypothesis(statement=statement, proposed_by="agent")

    # Log the action
    KS.log_analysis(action="add_hypothesis", hypothesis_id=hypothesis_id, statement=statement)
    KS.save(JOB_DIR / "knowledge_state.json")

    return f"✅ Hypothesis recorded as {hypothesis_id}: {statement}"


@mcp.tool()
def update_hypothesis(
    hypothesis_id: str,
    status: str,
    result_summary: str = "",
    p_value: str = "",
    effect_size: str = "",
    conclusion: str = "",
) -> str:
    """
    Update a hypothesis with test results.

    Call this after testing a hypothesis to record the outcome.

    Args:
        hypothesis_id: The hypothesis ID (e.g., "H001")
        status: New status - must be one of:
                - "testing" - currently being tested
                - "supported" - evidence supports the hypothesis
                - "rejected" - evidence contradicts the hypothesis
        result_summary: Brief summary of test results
        p_value: P-value from statistical test (as string, e.g., "p=0.003")
        effect_size: Effect size (e.g., "Cohen's d=0.8", "r=0.45")
        conclusion: What this means for the research question

    Returns:
        Confirmation message
    """
    global KS
    assert JOB_DIR is not None, "JOB_DIR not initialized"

    # Validate status
    valid_statuses = ["pending", "testing", "supported", "rejected"]
    if status not in valid_statuses:
        return f"❌ Invalid status '{status}'. Must be one of: {', '.join(valid_statuses)}"

    # Reload knowledge graph to get latest state
    KS = KnowledgeState.load(JOB_DIR / "knowledge_state.json")

    # Build updates dict
    updates: Dict[str, Any] = {"status": status}

    if status in ["supported", "rejected"]:
        updates["tested_at_iteration"] = KS.data["iteration"]
        updates["result"] = {
            "summary": result_summary,
            "p_value": p_value,
            "effect_size": effect_size,
            "conclusion": conclusion,
        }

    # Update hypothesis
    try:
        KS.update_hypothesis(hypothesis_id, updates)
    except ValueError as e:
        return f"❌ {e}"

    # Log the action
    KS.log_analysis(
        action="update_hypothesis",
        hypothesis_id=hypothesis_id,
        status=status,
        result_summary=result_summary,
    )
    KS.save(JOB_DIR / "knowledge_state.json")

    status_emoji = {"testing": "🔬", "supported": "✅", "rejected": "❌"}.get(status, "📝")
    return f"{status_emoji} Hypothesis {hypothesis_id} updated to '{status}'"


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
    global KS
    assert JOB_DIR is not None, "JOB_DIR not initialized"

    # Reload knowledge graph to get latest state
    KS = KnowledgeState.load(JOB_DIR / "knowledge_state.json")

    # Get current iteration
    current_iteration = int(KS.data["iteration"])

    # Save the summary with strapline
    KS.add_iteration_summary(current_iteration, summary, strapline=strapline)
    KS.save(JOB_DIR / "knowledge_state.json")

    return f"✅ Summary saved for iteration {current_iteration}"


@mcp.tool()
def set_status(message: str) -> str:
    """
    Update your current status to show what you're working on.

    Call this tool whenever you start a new activity to keep the user informed
    of your progress. The status message appears in the job detail page.

    Examples of good status messages:
    - "Searching for caffeine-ADHD interaction studies"
    - "Analyzing gene expression correlation with treatment"
    - "Reviewing literature on dopamine receptor mechanisms"
    - "Running statistical tests on metabolite data"
    - "Preparing iteration summary"

    Keep messages brief (under 80 characters) and descriptive of the current task.

    Args:
        message: Brief description of what you're currently doing

    Returns:
        Confirmation message
    """
    global KS
    assert JOB_DIR is not None, "JOB_DIR not initialized"

    # Reload knowledge graph to get latest state
    KS = KnowledgeState.load(JOB_DIR / "knowledge_state.json")

    # Set the status
    KS.set_agent_status(message)
    KS.save(JOB_DIR / "knowledge_state.json")

    return "✓ Status updated"


@mcp.tool()
def set_job_title(title: str) -> str:
    """
    Set a brief, descriptive title for this job.

    Call this early in your investigation to give the job a meaningful title
    that summarizes its focus. The title should be concise (under 100 characters)
    and capture the essence of the research question.

    Good examples:
    - "Kinase inhibitor binding analysis"
    - "Metabolomic response to hypoxia"
    - "Structural basis of enzyme specificity"

    Bad examples (too long/vague):
    - "Analysis of the data" (too vague)
    - "Investigation into the mechanisms..." (too long)

    Args:
        title: Brief title for the job (max 100 characters)

    Returns:
        Confirmation message
    """
    import json

    assert JOB_DIR is not None, "JOB_DIR not initialized"

    # Validate title length
    if len(title) > 100:
        return f"❌ Title too long ({len(title)} chars). Please keep it under 100 characters."

    if len(title) < 3:
        return "❌ Title too short. Please provide a meaningful title."

    # Update config.json
    config_path = JOB_DIR / "config.json"
    if config_path.exists():
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
        config["short_title"] = title
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)

    # Update database asynchronously
    try:
        from uuid import UUID

        from shandy.database.session import AsyncSessionLocal

        job_id = JOB_DIR.name

        async def update_db():
            from sqlalchemy import update

            from shandy.database.models import Job

            async with AsyncSessionLocal(thread_safe=True) as session:
                stmt = update(Job).where(Job.id == UUID(job_id)).values(short_title=title)
                await session.execute(stmt)
                await session.commit()

        async def update_db_with_error_handling():
            try:
                await update_db()
            except Exception as e:
                print(f"Warning: Background database update failed: {e}", file=sys.stderr)

        import asyncio

        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.create_task(update_db_with_error_handling())
        else:
            asyncio.run(update_db())
    except Exception as e:
        # Log but don't fail - config.json is the primary source
        print(f"Warning: Failed to update database with title: {e}", file=sys.stderr)

    return f"✓ Job title set to: {title}"


@mcp.tool()
def search_skills(query: str, add_to_job: bool = False, max_results: int = 3) -> str:
    """
    Search for domain-specific skills to guide your analysis.

    Skills are curated knowledge documents containing best practices,
    methodologies, and domain expertise. Use this tool when you need
    guidance on specific analysis techniques or domains you're less
    familiar with.

    Args:
        query: Search query describing what kind of guidance you need
               (e.g., "metabolomics pathway analysis", "statistical testing")
        add_to_job: If True, add matching skills to this job for future reference.
                    Added skills persist and appear in the job's skills list.
        max_results: Maximum number of skills to return (default: 3, max: 5)

    Returns:
        Formatted list of matching skills with their content, or a message
        if no skills match the query or skill limit is reached.
    """
    import asyncio

    from shandy.database.models import JobSkill
    from shandy.database.session import AsyncSessionLocal
    from shandy.prompts import format_skills_content, get_relevant_skills_with_scores
    from shandy.settings import get_settings

    assert JOB_DIR is not None, "JOB_DIR not initialized"

    # Limit max_results to prevent abuse
    max_results = min(max_results, 5)

    job_id = JOB_DIR.name

    async def _search_and_add() -> str:
        settings = get_settings()
        max_skills = settings.agent.max_agent_skills

        async with AsyncSessionLocal(thread_safe=True) as session:
            # Check current skill count for this job
            from sqlalchemy import func, select

            count_stmt = (
                select(func.count()).select_from(JobSkill).where(JobSkill.job_id == UUID(job_id))
            )
            result = await session.execute(count_stmt)
            current_count = result.scalar() or 0

            if add_to_job and current_count >= max_skills:
                return (
                    f"❌ Cannot add more skills: Job has reached the maximum of "
                    f"{max_skills} skills. Consider reviewing existing skills instead."
                )

            # Search for relevant skills with scores
            skills_with_scores = await get_relevant_skills_with_scores(
                session, query, limit=max_results
            )

            if not skills_with_scores:
                return f"No skills found matching '{query}'. Try a different search query."

            skills = [s for s, _ in skills_with_scores]

            # If add_to_job, persist new skills (avoiding duplicates)
            added_count = 0
            if add_to_job:
                # Get existing skill IDs for this job
                existing_stmt = select(JobSkill.skill_id).where(JobSkill.job_id == UUID(job_id))
                existing_result = await session.execute(existing_stmt)
                existing_skill_ids = {row[0] for row in existing_result.fetchall()}

                for skill, score in skills_with_scores:
                    if current_count + added_count >= max_skills:
                        break
                    if skill.id not in existing_skill_ids:
                        job_skill = JobSkill(
                            job_id=UUID(job_id),
                            skill_id=skill.id,
                            skill_name=skill.name,
                            skill_category=skill.category,
                            skill_content=skill.content,
                            source="agent",
                            similarity_score=score,
                        )
                        session.add(job_skill)
                        added_count += 1

                if added_count > 0:
                    await session.commit()

            # Format skills for display
            formatted = format_skills_content(skills)

            if add_to_job:
                if added_count > 0:
                    header = f"✅ Added {added_count} skill(s) to this job:\n\n"
                else:
                    header = "ℹ️ Skills found (already added to this job):\n\n"
            else:
                header = f"Found {len(skills)} skill(s) matching '{query}':\n\n"

            return header + formatted

    # Run async function using the same pattern as job_manager._run_async
    # This properly handles running async code from sync contexts,
    # including cases where we might be inside an existing event loop
    def _run_async_safely(coro: Any) -> str:
        """Run async coroutine safely from any sync context."""
        import concurrent.futures

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None:
            # We're inside a running event loop - run in a separate thread
            # with its own event loop. The coroutine will create its own
            # database session via AsyncSessionLocal(thread_safe=True).
            def run_in_thread() -> str:
                result: str = asyncio.run(coro)
                return result

            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(run_in_thread)
                result: str = future.result(timeout=30)
                return result
        else:
            # No running loop - create one
            result = asyncio.run(coro)
            return result

    try:
        return _run_async_safely(_search_and_add())
    except Exception as e:
        return f"❌ Error searching skills: {e}"


@mcp.tool()
def read_document(file_path: str) -> str:
    """
    Read and extract text from a document file (PDF, DOCX, XLSX).

    Use this tool to read binary document formats that cannot be read directly
    with Claude's Read tool. The Read tool returns garbled binary content for
    PDFs and other binary formats, which corrupts your context.

    IMPORTANT: Use this tool for:
    - PDF files (.pdf)
    - Word documents (.docx)
    - Excel files (.xlsx) - for a text overview; use execute_code with pandas for data analysis

    You can still use Claude's Read tool directly for text-based files:
    - CSV, TSV, TXT, JSON, Markdown, etc.

    Args:
        file_path: Path to the document file (can be relative to job data directory)

    Returns:
        Extracted text content from the document
    """
    assert JOB_DIR is not None, "JOB_DIR not initialized"

    path = Path(file_path)

    # If path is not absolute, try to resolve relative to job data directory
    if not path.is_absolute():
        # Try job data directory first
        job_data_path = JOB_DIR / "data" / path.name
        if job_data_path.exists():
            path = job_data_path
        else:
            # Try as-is (might be relative to cwd)
            path = Path(file_path)

    if not path.exists():
        # List available files to help the user
        data_dir = JOB_DIR / "data"
        if data_dir.exists():
            available = [f.name for f in data_dir.iterdir() if f.is_file()]
            return (
                f"Error: File not found: {file_path}\n\n"
                f"Available files in data directory:\n"
                + "\n".join(f"  - {name}" for name in available)
            )
        return f"Error: File not found: {file_path}"

    return read_doc(path)


def main():
    """Main entry point for MCP server."""
    import argparse

    from shandy.version import get_version_string

    # Log version info at startup
    print("=" * 60, file=sys.stderr)
    print(get_version_string(), file=sys.stderr)
    print("=" * 60, file=sys.stderr)

    # Validate settings at startup
    try:
        from shandy.settings import get_settings

        get_settings()  # Validates and caches settings
        print("✅ Settings validated successfully", file=sys.stderr)
    except Exception as e:
        print(f"❌ Configuration error: {e}", file=sys.stderr)
        sys.exit(1)

    parser = argparse.ArgumentParser(description="SHANDY MCP Server")
    parser.add_argument("--job-dir", required=True, help="Job directory")
    parser.add_argument(
        "--data-file", required=False, default=None, help="Primary data file (optional)"
    )

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
        except (ValueError, OSError) as e:
            print(
                f"❌ ERROR: Could not read file info for {primary_file}: {e}",
                file=sys.stderr,
            )
            sys.exit(1)

        # Log file info (but don't load data yet)
        file_size_mb = primary_info["size"] / (1024 * 1024)
        print(
            f"📂 Data file registered: {primary_file.name} ({file_size_mb:.1f} MB) - will load on first use",
            file=sys.stderr,
        )
    else:
        # No primary data file
        DATA_FILE_PATH = None
        DATA_FILES = []
        print(
            "ℹ️  No data file provided - server running in no-data mode",
            file=sys.stderr,
        )

    # Scan job data directory for additional files (metadata only)
    data_dir = JOB_DIR / "data"
    if data_dir.exists():
        for file_path in data_dir.iterdir():
            if file_path.is_file() and file_path != primary_file:
                try:
                    file_info = get_file_info(file_path)
                    DATA_FILES.append(file_info)
                except (ValueError, OSError, FileLoadError) as e:
                    print(f"Warning: Could not process {file_path}: {e}", file=sys.stderr)

    print(
        f"📁 {len(DATA_FILES)} data file(s) available: {', '.join(f['name'] for f in DATA_FILES)}",
        file=sys.stderr,
    )

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
