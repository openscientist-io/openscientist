"""
Orchestrator for SHANDY autonomous discovery.

Spawns Claude Code CLI to run autonomous discovery loop.
"""

import fcntl
import json
import logging
import os
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv

from .file_loader import get_file_info
from .knowledge_state import KnowledgeState
from .providers import get_provider

# Load environment variables (important for Claude CLI subprocess)
if not load_dotenv("/app/.env", override=True):
    load_dotenv(".env", override=True)

logger = logging.getLogger(__name__)


FEEDBACK_TIMEOUT_SECONDS = 15 * 60  # 15 minutes


def get_version_metadata() -> Dict[str, str]:
    """
    Get SHANDY version metadata for reproducibility.

    Returns:
        Dict with shandy_commit, shandy_build_time, and docker_container_id
    """
    metadata = {}

    # Try environment variables first (set during Docker build)
    shandy_commit = os.environ.get('SHANDY_COMMIT')
    if shandy_commit and shandy_commit != 'unknown':
        metadata['shandy_commit'] = shandy_commit[:12]

    shandy_build_time = os.environ.get('SHANDY_BUILD_TIME')
    if shandy_build_time and shandy_build_time != 'unknown':
        metadata['shandy_build_time'] = shandy_build_time

    # Fallback to git if not in Docker
    if 'shandy_commit' not in metadata:
        try:
            result = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                metadata['shandy_commit'] = result.stdout.strip()[:12]
        except Exception:
            pass

    # Get docker container ID (if running in Docker)
    try:
        if Path('/.dockerenv').exists():
            # Read hostname which is the container ID in Docker
            with open('/etc/hostname', 'r') as f:
                container_id = f.read().strip()
                if container_id:
                    metadata['docker_container_id'] = container_id[:12]
    except Exception:
        pass

    return metadata


def extract_claude_info_from_transcript(transcript: list) -> Dict[str, str]:
    """
    Extract Claude model and version info from stream-json transcript.

    The init message contains model and claude_code_version fields.

    Args:
        transcript: List of parsed JSON objects from stream-json output

    Returns:
        Dict with claude_model and claude_code_version
    """
    info = {}

    for msg in transcript:
        if msg.get('type') == 'system' and msg.get('subtype') == 'init':
            if 'model' in msg:
                info['claude_model'] = msg['model']
            if 'claude_code_version' in msg:
                info['claude_code_version'] = msg['claude_code_version']
            break

    return info


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

    config_path = job_dir / "config.json"
    ks_path = job_dir / "knowledge_state.json"

    start_time = time.time()
    last_feedback_count = 0

    # Get initial feedback count
    ks =KnowledgeState.load(ks_path)
    current_iteration = ks.data["iteration"]
    last_feedback_count = len(ks.data.get("feedback_history", []))

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
        ks =KnowledgeState.load(ks_path)
        feedback_history = ks.data.get("feedback_history", [])

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
    # Track when we started awaiting feedback (for countdown timer)
    if status == "awaiting_feedback":
        config["awaiting_feedback_since"] = datetime.now().isoformat()
    elif "awaiting_feedback_since" in config:
        del config["awaiting_feedback_since"]
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)


def parse_stream_json(stdout: str) -> list:
    """
    Parse stream-json output (one JSON object per line).

    Strips large data fields (images, file contents) from each line BEFORE
    parsing to avoid JSON parse errors and keep transcripts small.

    Args:
        stdout: Raw stdout from Claude CLI with --output-format stream-json

    Returns:
        List of parsed JSON objects (the transcript)
    """
    import re

    transcript = []
    for line in stdout.strip().split('\n'):
        if not line:
            continue

        # Strip "data" fields with long content (images, file contents)
        # These are raw file bytes we don't need in the transcript.
        # Pattern: "data": "..." where string is >1000 chars
        sanitized_line = re.sub(
            r'"data":\s*"[^"]{1000,}"',
            '"data": "[CONTENT REMOVED]"',
            line
        )

        try:
            obj = json.loads(sanitized_line)
            transcript.append(obj)
        except json.JSONDecodeError as e:
            logger.warning(f"Skipping unparseable JSON line (error at pos {e.pos}): {line[:100]}...")

    return transcript


def increment_ks_iteration(ks_path: Path) -> None:
    """
    Safely increment the knowledge graph iteration counter with file locking.

    This ensures mutual exclusion with MCP server writes to prevent race conditions.

    Args:
        ks_path: Path to knowledge_state.json
    """
    with open(ks_path, 'r+') as f:
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
    (job_dir / "provenance").mkdir(exist_ok=True)

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
    ks =KnowledgeState(
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
        ks.set_data_summary({
            "files": [str(p.name) for p in data_paths],
            "file_type": file_info["file_type"],
            "file_size_mb": file_info["size"] / (1024 * 1024)
        })
    else:
        # No data files provided
        ks.set_data_summary({
            "files": [],
            "file_type": "none",
            "file_size_mb": 0
        })

    ks.save(job_dir / "knowledge_state.json")

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
        ks =KnowledgeState.load(job_dir / "knowledge_state.json")

        # Build data context based on whether files were provided
        if config['data_files']:
            data_context = f"""Data summary:
- Files: {config['data_files']}
- Columns: {ks.data['data_summary'].get('columns', [])}
- Samples: {ks.data['data_summary'].get('n_samples', 'Unknown')}"""
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
- update_knowledge_state: Record confirmed findings with statistical evidence
- save_iteration_summary: Record a summary of what you investigated and learned

IMPORTANT: At the end of each iteration, call save_iteration_summary with a 1-2 sentence
plain-language summary of what you investigated and what you learned. This helps users
understand your investigation progress.

Start your investigation by using these tools to analyze the data.
"""

        logger.info("Starting discovery loop with Claude CLI headless mode")

        # Iteration 1: Start session
        # Note: Pass prompt via stdin to avoid ARG_MAX limits with large prompts
        cmd = [
            claude_cli,
            '-p',
            '--verbose',
            '--output-format', 'stream-json',
            '--mcp-config', str(mcp_config_path.absolute()),
            '--allowedTools', 'Skill',  # Enable skill invocation for domain-specific workflows
            '--allowedTools', 'mcp__shandy-tools__execute_code',
            '--allowedTools', 'mcp__shandy-tools__search_pubmed',
            '--allowedTools', 'mcp__shandy-tools__update_knowledge_state',
            '--allowedTools', 'mcp__shandy-tools__save_iteration_summary',
            '--allowedTools', 'mcp__shandy-tools__read_document',
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

        # Parse stream-json output (one JSON object per line)
        # This sanitizes base64 image data during parsing to avoid corruption issues
        transcript = parse_stream_json(result.stdout)

        # Find the result item to get session_id
        result_item = next((item for item in transcript if item.get('type') == 'result'), None)
        session_id = result_item.get('session_id') if result_item else None
        logger.info(f"Iteration 1 completed successfully (session: {session_id})")

        # Extract and save version metadata for reproducibility
        version_info = get_version_metadata()
        claude_info = extract_claude_info_from_transcript(transcript)
        version_info.update(claude_info)
        if version_info:
            ks = KnowledgeState.load(job_dir / "knowledge_state.json")
            ks.set_version_info(version_info)
            ks.save(job_dir / "knowledge_state.json")
            logger.info(f"Saved version info: {version_info}")

        # Save full transcript to provenance/ for scientific reproducibility
        # (base64 image data already stripped during parsing)
        provenance_dir = job_dir / "provenance"
        provenance_dir.mkdir(parents=True, exist_ok=True)
        transcript_file = provenance_dir / "iter1_transcript.json"
        with open(transcript_file, "w") as f:
            json.dump(transcript, f, indent=2)
        logger.info(f"Saved transcript to {transcript_file}")

        # Log iteration (human-readable summary)
        log_file = job_dir / "claude_iterations.log"
        with open(log_file, "w") as f:
            f.write("=== Iteration 1 ===\n")
            f.write(f"Prompt: {initial_prompt}\n\n")
            f.write(f"Response: {json.dumps(result_item, indent=2)}\n\n")

        # Increment iteration counter with file locking to prevent race conditions
        # Only increment if there are more iterations to come
        ks_path = job_dir / "knowledge_state.json"
        if max_iterations > 1:
            increment_ks_iteration(ks_path)

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
        RESET_INTERVAL = 5  # Start fresh session every N iterations  # noqa: N806

        for iteration in range(2, max_iterations + 1):
            # Reload knowledge graph to see latest state
            ks =KnowledgeState.load(job_dir / "knowledge_state.json")

            # Check for feedback from previous iteration (from KS if not already captured)
            if pending_feedback is None:
                pending_feedback = ks.get_feedback_for_iteration(iteration)

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
{ks.get_summary()}

---

Continue your investigation using the available MCP tools.
Examples: execute_code, search_pubmed, update_knowledge_state, save_iteration_summary.
Think step by step about what will provide the most insight, then actively use the tools to execute your investigation.

Remember: At the end of this iteration, call save_iteration_summary with a brief summary of what you investigated and learned."""

            # Decide whether to resume or start fresh
            should_reset = (iteration % RESET_INTERVAL == 1)

            if should_reset:
                logger.info(f"Iteration {iteration}/{max_iterations}: Starting fresh session (context reset)")
                cmd = [
                    claude_cli,
                    '-p',
                    '--verbose',
                    '--output-format', 'stream-json',
                    '--mcp-config', str(mcp_config_path.absolute()),
                    '--allowedTools', 'Skill',  # Enable skill invocation for domain-specific workflows
                    '--allowedTools', 'mcp__shandy-tools__execute_code',
                    '--allowedTools', 'mcp__shandy-tools__search_pubmed',
                    '--allowedTools', 'mcp__shandy-tools__update_knowledge_state',
                    '--allowedTools', 'mcp__shandy-tools__save_iteration_summary',
                    '--allowedTools', 'mcp__shandy-tools__read_document',
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
                    '--verbose',
                    '--output-format', 'stream-json',
                    '--mcp-config', str(mcp_config_path.absolute()),
                    '--allowedTools', 'Skill',  # Enable skill invocation for domain-specific workflows
                    '--allowedTools', 'mcp__shandy-tools__execute_code',
                    '--allowedTools', 'mcp__shandy-tools__search_pubmed',
                    '--allowedTools', 'mcp__shandy-tools__update_knowledge_state',
                    '--allowedTools', 'mcp__shandy-tools__save_iteration_summary',
                    '--allowedTools', 'mcp__shandy-tools__read_document',
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

            # Parse stream-json output (one JSON object per line)
            # This sanitizes base64 image data during parsing to avoid corruption issues
            transcript = parse_stream_json(result.stdout)

            # Find the result item to get session_id
            result_item = next((item for item in transcript if item.get('type') == 'result'), None)

            # Update session_id if we started a fresh session
            if should_reset:
                new_session_id = result_item.get('session_id') if result_item else None
                if new_session_id:
                    session_id = new_session_id
                    logger.info(f"New session started: {session_id}")

            # Save full transcript to provenance/ for scientific reproducibility
            # (base64 image data already stripped during parsing)
            transcript_file = provenance_dir / f"iter{iteration}_transcript.json"
            with open(transcript_file, "w") as f:
                json.dump(transcript, f, indent=2)
            logger.info(f"Saved transcript to {transcript_file}")

            # Log iteration (human-readable summary)
            with open(log_file, "a") as f:
                f.write(f"=== Iteration {iteration} ===\n")
                f.write(f"Prompt: {iteration_prompt}\n\n")
                f.write(f"Response: {json.dumps(result_item, indent=2)}\n\n")

            # Increment iteration counter with file locking to prevent race conditions
            # Only increment if this is not the last iteration
            if iteration < max_iterations:
                increment_ks_iteration(ks_path)

            # Coinvestigate mode: wait for feedback after each iteration (except last)
            if investigation_mode == "coinvestigate" and iteration < max_iterations:
                update_job_status(job_dir, "awaiting_feedback")
                pending_feedback = wait_for_feedback_or_timeout(job_dir)
                update_job_status(job_dir, "running")

        logger.info("Discovery loop completed")

        # Generate final report using Claude
        logger.info("Generating final report...")
        ks =KnowledgeState.load(job_dir / "knowledge_state.json")

        # Build concise summary instead of full JSON dump
        findings_summary = "\n\n".join([
            f"**Finding {i+1}: {f['title']}**\n"
            f"Evidence: {f['evidence']}\n"
            f"Interpretation: {f.get('biological_interpretation', 'N/A')}"
            for i, f in enumerate(ks.data['findings'])
        ])

        literature_summary = f"Reviewed {len(ks.data['literature'])} papers from PubMed"

        report_prompt = f"""You have completed autonomous discovery. Generate a final report summarizing your findings.

Research Question: {config['research_question']}

## Iterations Completed: {ks.data['iteration']}

## Findings ({len(ks.data['findings'])}):
{findings_summary}

## Literature: {literature_summary}

## Analysis Log: {len(ks.data['analysis_log'])} actions performed across {ks.data['iteration']} iterations

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

        report_generated = False
        report_error = None

        if result.returncode == 0:
            report_content = result.stdout
            # Save Markdown report
            markdown_path = job_dir / "final_report.md"
            with open(markdown_path, "w") as f:
                f.write(report_content)
            logger.info("Final report (Markdown) generated")
            report_generated = True

            # Generate PDF version
            try:
                from .pdf_generator import markdown_to_pdf
                pdf_path = markdown_to_pdf(markdown_path, add_footer=True)
                logger.info(f"Final report (PDF) generated: {pdf_path}")
            except Exception as e:
                logger.warning(f"PDF generation failed (Markdown still available): {e}")
        else:
            report_error = result.stdout[:1000] if result.stdout else result.stderr[:1000] if result.stderr else "Unknown error"
            logger.error(f"Report generation failed (rc={result.returncode})")
            logger.error(f"  stderr: {result.stderr}")
            logger.error(f"  stdout: {result.stdout[:1000]}")

        # Load final knowledge graph
        ks = KnowledgeState.load(job_dir / "knowledge_state.json")

        # Update job status - mark as failed if report generation failed
        if report_generated:
            config["status"] = "completed"
        else:
            config["status"] = "failed"
            config["error"] = f"Report generation failed: {report_error}"

        config["completed_at"] = datetime.now().isoformat()
        config["iterations_completed"] = ks.data["iteration"]
        config["findings_count"] = len(ks.data["findings"])
        # Update max_iterations to actual iterations when job stops early
        config["max_iterations"] = ks.data["iteration"]

        with open(job_dir / "config.json", "w") as f:
            json.dump(config, f, indent=2)

        return {
            "job_id": job_id,
            "status": config["status"],
            "iterations": ks.data["iteration"],
            "findings": len(ks.data["findings"])
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

    print("\nDiscovery complete!")
    print(f"Job ID: {result['job_id']}")
    print(f"Iterations: {result['iterations']}")
    print(f"Findings: {result['findings']}")


if __name__ == "__main__":
    main()
