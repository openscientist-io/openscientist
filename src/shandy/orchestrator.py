"""
Orchestrator for SHANDY autonomous discovery.

Spawns Claude Code CLI to run autonomous discovery loop.
"""

import fcntl
import json
import logging
import os
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
import pandas as pd
from dotenv import load_dotenv

from .knowledge_graph import KnowledgeGraph
from .providers import get_provider
from .file_loader import load_data_file, get_file_info

# Load environment variables (important for Claude CLI subprocess)
if not load_dotenv("/app/.env", override=True):
    load_dotenv(".env", override=True)

logger = logging.getLogger(__name__)


FEEDBACK_TIMEOUT_SECONDS = 15 * 60  # 15 minutes


def wait_for_feedback_or_timeout(job_dir: Path, timeout_seconds: int = FEEDBACK_TIMEOUT_SECONDS) -> Optional[str]:
    """
    Wait for scientist feedback or timeout (coinvestigate mode).

    Polls the knowledge graph for new feedback entries. Returns when:
    - Feedback is submitted (returns feedback text)
    - Timeout expires (returns None)
    - Job is cancelled (returns None)

    Args:
        job_dir: Path to job directory
        timeout_seconds: How long to wait before auto-continuing

    Returns:
        Feedback text if submitted, None if timeout or cancelled
    """
    import time

    config_path = job_dir / "config.json"
    kg_path = job_dir / "knowledge_graph.json"

    start_time = time.time()
    last_feedback_count = 0

    # Get initial feedback count
    kg = KnowledgeGraph.load(kg_path)
    current_iteration = kg.data["iteration"]
    last_feedback_count = len(kg.data.get("feedback_history", []))

    logger.info(f"Waiting for scientist feedback (timeout: {timeout_seconds}s)")

    while True:
        elapsed = time.time() - start_time

        # Check timeout
        if elapsed >= timeout_seconds:
            logger.info(f"Feedback timeout after {elapsed:.0f}s - auto-continuing")
            return None

        # Check if job was cancelled
        with open(config_path) as f:
            config = json.load(f)
        if config.get("status") == "cancelled":
            logger.info("Job cancelled while waiting for feedback")
            return None

        # Check for new feedback
        kg = KnowledgeGraph.load(kg_path)
        feedback_history = kg.data.get("feedback_history", [])

        if len(feedback_history) > last_feedback_count:
            # New feedback added - check if it's for current iteration
            latest = feedback_history[-1]
            if latest.get("after_iteration") == current_iteration:
                logger.info(f"Received feedback: {latest['text'][:100]}...")
                return latest["text"]

        # Check for continue signal (status changed back to running)
        with open(config_path) as f:
            config = json.load(f)
        if config.get("status") == "running":
            logger.info("Continue signal received (no feedback)")
            return None

        # Sleep before next poll
        time.sleep(2)


def update_job_status(job_dir: Path, status: str) -> None:
    """Update job status in config.json."""
    config_path = job_dir / "config.json"
    with open(config_path) as f:
        config = json.load(f)
    config["status"] = status
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)


def increment_kg_iteration(kg_path: Path) -> None:
    """
    Safely increment the knowledge graph iteration counter with file locking.

    This ensures mutual exclusion with MCP server writes to prevent race conditions.

    Args:
        kg_path: Path to knowledge_graph.json
    """
    with open(kg_path, 'r+') as f:
        # Acquire exclusive lock
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            # Read current state
            kg_data = json.load(f)

            # Increment iteration
            kg_data['iteration'] += 1

            # Write back atomically
            f.seek(0)
            f.truncate()
            json.dump(kg_data, f, indent=2)
        finally:
            # Release lock
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def create_job(job_id: str, research_question: str, data_files: list,
               max_iterations: int, use_skills: bool = True,
               jobs_dir: Path = Path("jobs"),
               investigation_mode: str = "autonomous") -> Path:
    """
    Create a new discovery job.

    Args:
        job_id: Unique job identifier
        research_question: User's research question
        data_files: List of uploaded data file paths
        max_iterations: Maximum number of iterations
        use_skills: Whether to use skills
        jobs_dir: Base directory for jobs
        investigation_mode: "autonomous" (default) or "coinvestigate"

    Returns:
        Path to job directory
    """
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    (job_dir / "data").mkdir(exist_ok=True)
    (job_dir / "plots").mkdir(exist_ok=True)

    # Copy data files to job directory, preserving original names/extensions (if any)
    data_paths = []
    if data_files:
        for data_file in data_files:
            # Preserve original filename
            original_name = Path(data_file).name
            dest = job_dir / "data" / original_name
            # In real implementation, handle file upload properly
            # For now, assume data_file is already a path
            import shutil
            shutil.copy(data_file, dest)
            data_paths.append(dest)

    # Initialize knowledge graph
    kg = KnowledgeGraph(
        job_id=job_id,
        research_question=research_question,
        max_iterations=max_iterations,
        use_skills=use_skills
    )

    # Add data summary - metadata only, no data loading!
    # This keeps job creation fast (no 30-40s wait for large files)
    # Data will be loaded on-demand by MCP server when first needed
    if data_paths:
        first_file = data_paths[0]

        # Get file type (fast - just checks extension and file size)
        file_info = get_file_info(first_file)

        # Set minimal data summary (agent will discover details when analyzing)
        kg.set_data_summary({
            "files": [str(p.name) for p in data_paths],
            "file_type": file_info["file_type"],
            "file_size_mb": file_info["size"] / (1024 * 1024)
        })
    else:
        # No data files provided
        kg.set_data_summary({
            "files": [],
            "file_type": "none",
            "file_size_mb": 0
        })

    kg.save(job_dir / "knowledge_graph.json")

    # Save job config
    config = {
        "job_id": job_id,
        "research_question": research_question,
        "data_files": [str(p) for p in data_paths],
        "max_iterations": max_iterations,
        "use_skills": use_skills,
        "investigation_mode": investigation_mode,
        "created_at": datetime.now().isoformat(),
        "status": "created"
    }

    with open(job_dir / "config.json", "w") as f:
        json.dump(config, f, indent=2)

    logger.info(f"Created job {job_id} at {job_dir}")
    return job_dir


def run_discovery(job_dir: Path) -> Dict[str, Any]:
    """
    Run autonomous discovery using Claude Code CLI.

    Args:
        job_dir: Path to job directory

    Returns:
        Dictionary with job results
    """
    job_dir = Path(job_dir)

    # Load job config
    with open(job_dir / "config.json") as f:
        config = json.load(f)

    job_id = config["job_id"]
    max_iterations = config["max_iterations"]
    investigation_mode = config.get("investigation_mode", "autonomous")
    data_file = Path(config["data_files"][0]) if config["data_files"] else None

    logger.info(f"Investigation mode: {investigation_mode}")

    # Ensure data_file is absolute (if present)
    if data_file and not data_file.is_absolute():
        data_file = data_file.absolute()

    logger.info(f"Starting discovery for job {job_id}")

    # Initialize provider and configure environment
    provider = get_provider()
    provider.setup_environment()

    # Create job-specific MCP config
    # Note: Add generous timeout for MCP server startup to handle large data files
    # Large Excel files (>30MB) can take 30-40 seconds to load into pandas
    mcp_args = [
        "-m", "shandy.mcp_server",
        "--job-dir", str(job_dir.absolute()),
    ]
    if data_file:
        mcp_args.extend(["--data-file", str(data_file.absolute())])

    mcp_config = {
        "mcpServers": {
            "shandy-tools": {
                "command": "python",
                "args": mcp_args,
                "timeout": 120  # 2 minute timeout for server startup (handles large file loading)
            }
        }
    }

    mcp_config_path = job_dir / "mcp_config.json"
    with open(mcp_config_path, "w") as f:
        json.dump(mcp_config, f, indent=2)

    # Update job status
    config["status"] = "running"
    config["started_at"] = datetime.now().isoformat()
    with open(job_dir / "config.json", "w") as f:
        json.dump(config, f, indent=2)

    # Run autonomous discovery loop using Claude Code CLI headless mode
    try:
        # Get Claude Code path
        claude_cli = os.getenv("CLAUDE_CLI_PATH", "claude")

        # Prepare initial prompt
        kg = KnowledgeGraph.load(job_dir / "knowledge_graph.json")

        # Build data context based on whether files were provided
        if config['data_files']:
            data_context = f"""Data summary:
- Files: {config['data_files']}
- Columns: {kg.data['data_summary'].get('columns', [])}
- Samples: {kg.data['data_summary'].get('n_samples', 'Unknown')}"""
        else:
            data_context = "No data files provided. You may use literature search and computational methods."

        initial_prompt = f"""Begin autonomous discovery for this research question:

{config['research_question']}

You will run for a maximum of {max_iterations} iterations.

{data_context}

You have access to MCP tools for analysis, literature search, and recording findings.
Examples include (there may be others - explore what's available):
- execute_code: Analyze data, run statistical tests, create visualizations
- search_pubmed: Search for relevant papers
- update_knowledge_graph: Record confirmed findings with statistical evidence
- save_iteration_summary: Record a summary of what you investigated and learned

IMPORTANT: At the end of each iteration, call save_iteration_summary with a 1-2 sentence
plain-language summary of what you investigated and what you learned. This helps users
understand your investigation progress.

Start your investigation by using these tools to analyze the data.
"""

        logger.info(f"Starting discovery loop with Claude CLI headless mode")

        # Iteration 1: Start session
        # Note: Pass prompt via stdin to avoid ARG_MAX limits with large prompts
        cmd = [
            claude_cli,
            '-p',
            '--output-format', 'json',
            '--mcp-config', str(mcp_config_path.absolute()),
            '--allowedTools', 'mcp__shandy-tools__execute_code',
            '--allowedTools', 'mcp__shandy-tools__search_pubmed',
            '--allowedTools', 'mcp__shandy-tools__update_knowledge_graph',
            '--allowedTools', 'mcp__shandy-tools__save_iteration_summary',
            '--allowedTools', 'mcp__shandy-tools__run_phenix_tool',
            '--allowedTools', 'mcp__shandy-tools__compare_structures',
            '--allowedTools', 'mcp__shandy-tools__parse_alphafold_confidence'
        ]

        logger.info(f"Iteration 1/{max_iterations}: Starting session")
        logger.info(f"Running command: {' '.join(cmd)}")
        logger.info(f"Prompt length: {len(initial_prompt)} characters")
        result = subprocess.run(cmd, input=initial_prompt, capture_output=True, text=True, cwd=str(Path.cwd()), env=os.environ.copy())

        logger.info(f"Claude CLI return code: {result.returncode}")
        logger.info(f"Claude CLI stdout length: {len(result.stdout)}")
        logger.info(f"Claude CLI stderr length: {len(result.stderr)}")

        # Log stderr to diagnose MCP server issues (always log, not just on failure)
        if result.stderr:
            # Log first 2000 chars to capture MCP server startup messages
            logger.info(f"Claude CLI stderr (first 2000 chars):\n{result.stderr[:2000]}")

        if result.returncode != 0:
            logger.error(f"Claude CLI stdout: {result.stdout[:500]}")
            logger.error(f"Claude CLI stderr (full): {result.stderr}")
            raise RuntimeError(f"Claude CLI failed (rc={result.returncode}): {result.stderr or result.stdout}")

        # Parse JSON output
        response_data = json.loads(result.stdout)
        session_id = response_data.get('session_id')
        logger.info(f"Iteration 1 completed successfully (session: {session_id})")

        # Log iteration
        log_file = job_dir / "claude_iterations.log"
        with open(log_file, "w") as f:
            f.write(f"=== Iteration 1 ===\n")
            f.write(f"Prompt: {initial_prompt}\n\n")
            f.write(f"Response: {json.dumps(response_data, indent=2)}\n\n")

        # Increment iteration counter with file locking to prevent race conditions
        # Only increment if there are more iterations to come
        kg_path = job_dir / "knowledge_graph.json"
        if max_iterations > 1:
            increment_kg_iteration(kg_path)

        # Coinvestigate mode: wait for feedback after first iteration
        pending_feedback = None
        if investigation_mode == "coinvestigate" and max_iterations > 1:
            update_job_status(job_dir, "awaiting_feedback")
            pending_feedback = wait_for_feedback_or_timeout(job_dir)
            update_job_status(job_dir, "running")

        # Iterations 2-N: Resume session within batches, reset every 5 iterations
        # Note: We use --resume for short-term memory but reset periodically to:
        # 1. Prevent unbounded context growth over 10+ iterations
        # 2. Reduce cost while maintaining continuity within batches
        # 3. Agent gets reasoning context within a batch (5 iterations)
        RESET_INTERVAL = 5  # Start fresh session every N iterations

        for iteration in range(2, max_iterations + 1):
            # Reload knowledge graph to see latest state
            kg = KnowledgeGraph.load(job_dir / "knowledge_graph.json")

            # Check for feedback from previous iteration (from KG if not already captured)
            if pending_feedback is None:
                pending_feedback = kg.get_feedback_for_iteration(iteration)

            # Build feedback section if we have feedback
            feedback_section = ""
            if pending_feedback:
                feedback_section = f"""
## Scientist Feedback
The scientist has provided the following guidance after reviewing your previous iteration:
> {pending_feedback}

Continue your investigation, taking this guidance into account. Use your judgment to balance
the scientist's suggestions with your own analysis of what will be most productive.

---
"""
                pending_feedback = None  # Clear after using

            # Build iteration prompt
            iteration_prompt = f"""# Iteration {iteration}/{max_iterations}
{feedback_section}
{kg.get_summary()}

---

Continue your investigation using the available MCP tools.
Examples: execute_code, search_pubmed, update_knowledge_graph, save_iteration_summary.
Think step by step about what will provide the most insight, then actively use the tools to execute your investigation.

Remember: At the end of this iteration, call save_iteration_summary with a brief summary of what you investigated and learned."""

            # Decide whether to resume or start fresh
            should_reset = (iteration % RESET_INTERVAL == 1)

            if should_reset:
                logger.info(f"Iteration {iteration}/{max_iterations}: Starting fresh session (context reset)")
                cmd = [
                    claude_cli,
                    '-p',
                    '--output-format', 'json',
                    '--mcp-config', str(mcp_config_path.absolute()),
                    '--allowedTools', 'mcp__shandy-tools__execute_code',
                    '--allowedTools', 'mcp__shandy-tools__search_pubmed',
                    '--allowedTools', 'mcp__shandy-tools__update_knowledge_graph',
                    '--allowedTools', 'mcp__shandy-tools__save_iteration_summary',
                    '--allowedTools', 'mcp__shandy-tools__run_phenix_tool',
                    '--allowedTools', 'mcp__shandy-tools__compare_structures',
                    '--allowedTools', 'mcp__shandy-tools__parse_alphafold_confidence'
                ]
            else:
                logger.info(f"Iteration {iteration}/{max_iterations}: Resuming session {session_id}")
                cmd = [
                    claude_cli,
                    '-p',
                    '--resume', session_id,
                    '--output-format', 'json',
                    '--mcp-config', str(mcp_config_path.absolute()),
                    '--allowedTools', 'mcp__shandy-tools__execute_code',
                    '--allowedTools', 'mcp__shandy-tools__search_pubmed',
                    '--allowedTools', 'mcp__shandy-tools__update_knowledge_graph',
                    '--allowedTools', 'mcp__shandy-tools__save_iteration_summary',
                    '--allowedTools', 'mcp__shandy-tools__run_phenix_tool',
                    '--allowedTools', 'mcp__shandy-tools__compare_structures',
                    '--allowedTools', 'mcp__shandy-tools__parse_alphafold_confidence'
                ]

            logger.info(f"Prompt length: {len(iteration_prompt)} characters")
            result = subprocess.run(cmd, input=iteration_prompt, capture_output=True, text=True, cwd=str(Path.cwd()), env=os.environ.copy())

            # Log stderr to diagnose MCP server issues (even on success)
            if result.stderr:
                logger.info(f"Iteration {iteration} Claude CLI stderr (first 2000 chars):\n{result.stderr[:2000]}")

            if result.returncode != 0:
                logger.error(f"Iteration {iteration} failed (rc={result.returncode})")
                logger.error(f"  stderr (full): {result.stderr}")
                logger.error(f"  stdout: {result.stdout[:1000]}")
                break

            # Parse response
            response_data = json.loads(result.stdout)

            # Update session_id if we started a fresh session
            if should_reset:
                new_session_id = response_data.get('session_id')
                if new_session_id:
                    session_id = new_session_id
                    logger.info(f"New session started: {session_id}")

            # Log iteration
            with open(log_file, "a") as f:
                f.write(f"=== Iteration {iteration} ===\n")
                f.write(f"Prompt: {iteration_prompt}\n\n")
                f.write(f"Response: {json.dumps(response_data, indent=2)}\n\n")

            # Increment iteration counter with file locking to prevent race conditions
            # Only increment if this is not the last iteration
            if iteration < max_iterations:
                increment_kg_iteration(kg_path)

            # Coinvestigate mode: wait for feedback after each iteration (except last)
            if investigation_mode == "coinvestigate" and iteration < max_iterations:
                update_job_status(job_dir, "awaiting_feedback")
                pending_feedback = wait_for_feedback_or_timeout(job_dir)
                update_job_status(job_dir, "running")

        logger.info(f"Discovery loop completed")

        # Generate final report using Claude
        logger.info("Generating final report...")
        kg = KnowledgeGraph.load(job_dir / "knowledge_graph.json")

        # Build concise summary instead of full JSON dump
        findings_summary = "\n\n".join([
            f"**Finding {i+1}: {f['title']}**\n"
            f"Evidence: {f['evidence']}\n"
            f"Interpretation: {f.get('biological_interpretation', 'N/A')}"
            for i, f in enumerate(kg.data['findings'])
        ])

        literature_summary = f"Reviewed {len(kg.data['literature'])} papers from PubMed"

        report_prompt = f"""You have completed autonomous discovery. Generate a final report summarizing your findings.

Research Question: {config['research_question']}

## Iterations Completed: {kg.data['iteration']}

## Findings ({len(kg.data['findings'])}):
{findings_summary}

## Literature: {literature_summary}

## Analysis Log: {len(kg.data['analysis_log'])} actions performed across {kg.data['iteration']} iterations

Please create a comprehensive markdown report with:
1. Executive Summary (2-3 paragraphs)
2. Key Findings (with statistical evidence)
3. Mechanistic Model/Interpretation
4. Knowledge Gaps Identified
5. Proposed Follow-up Experiments

Format as professional scientific markdown."""

        # Generate report (single call, no session needed)
        # Note: Pass prompt via stdin to avoid ARG_MAX limits with large knowledge graphs
        cmd = [
            claude_cli,
            '-p',
            '--output-format', 'text'
        ]

        logger.info(f"Report prompt length: {len(report_prompt)} characters")
        result = subprocess.run(cmd, input=report_prompt, capture_output=True, text=True, cwd=str(Path.cwd()), env=os.environ.copy())

        if result.returncode == 0:
            report_content = result.stdout
            # Save Markdown report
            markdown_path = job_dir / "final_report.md"
            with open(markdown_path, "w") as f:
                f.write(report_content)
            logger.info("Final report (Markdown) generated")

            # Generate PDF version
            try:
                from .pdf_generator import markdown_to_pdf
                pdf_path = markdown_to_pdf(markdown_path, add_footer=True)
                logger.info(f"Final report (PDF) generated: {pdf_path}")
            except Exception as e:
                logger.warning(f"PDF generation failed (Markdown still available): {e}")
        else:
            logger.error(f"Report generation failed (rc={result.returncode})")
            logger.error(f"  stderr: {result.stderr}")
            logger.error(f"  stdout: {result.stdout[:1000]}")

        # Load final knowledge graph
        kg = KnowledgeGraph.load(job_dir / "knowledge_graph.json")

        # Update job status
        config["status"] = "completed"
        config["completed_at"] = datetime.now().isoformat()
        config["iterations_completed"] = kg.data["iteration"]
        config["findings_count"] = len(kg.data["findings"])
        # Update max_iterations to actual iterations when job stops early
        config["max_iterations"] = kg.data["iteration"]

        with open(job_dir / "config.json", "w") as f:
            json.dump(config, f, indent=2)

        return {
            "job_id": job_id,
            "status": "completed",
            "iterations": kg.data["iteration"],
            "findings": len(kg.data["findings"])
        }

    except Exception as e:
        logger.error(f"Discovery failed: {e}", exc_info=True)

        # Update job status
        config["status"] = "failed"
        config["error"] = str(e)
        config["failed_at"] = datetime.now().isoformat()

        with open(job_dir / "config.json", "w") as f:
            json.dump(config, f, indent=2)

        raise


def main():
    """CLI entry point for orchestrator."""
    import argparse

    parser = argparse.ArgumentParser(description="SHANDY Orchestrator")
    parser.add_argument("--job-dir", required=True, help="Job directory")
    args = parser.parse_args()

    # Set up logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # Run discovery
    result = run_discovery(Path(args.job_dir))

    print(f"\nDiscovery complete!")
    print(f"Job ID: {result['job_id']}")
    print(f"Iterations: {result['iterations']}")
    print(f"Findings: {result['findings']}")


if __name__ == "__main__":
    main()
