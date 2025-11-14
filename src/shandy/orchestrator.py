"""
Orchestrator for SHANDY autonomous discovery.

Spawns Claude Code CLI to run autonomous discovery loop.
"""

import json
import logging
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional
import pandas as pd

from .knowledge_graph import KnowledgeGraph
from .cost_tracker import get_cborg_spend, track_job_cost, BudgetExceededError

logger = logging.getLogger(__name__)


def create_job(job_id: str, research_question: str, data_files: list,
               max_iterations: int, use_skills: bool = True,
               jobs_dir: Path = Path("jobs")) -> Path:
    """
    Create a new discovery job.

    Args:
        job_id: Unique job identifier
        research_question: User's research question
        data_files: List of uploaded data file paths
        max_iterations: Maximum number of iterations
        use_skills: Whether to use skills
        jobs_dir: Base directory for jobs

    Returns:
        Path to job directory
    """
    job_dir = jobs_dir / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Create subdirectories
    (job_dir / "data").mkdir(exist_ok=True)
    (job_dir / "plots").mkdir(exist_ok=True)

    # Copy data files to job directory
    data_paths = []
    for i, data_file in enumerate(data_files):
        dest = job_dir / "data" / f"data_{i}.csv"
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

    # Add data summary
    # For now, just use first data file
    data = pd.read_csv(data_paths[0])
    kg.set_data_summary({
        "files": [str(p.name) for p in data_paths],
        "n_samples": len(data),
        "n_features": len(data.columns),
        "columns": list(data.columns),
        "groups": data.iloc[:, 1].unique().tolist() if len(data.columns) > 1 else []
    })

    kg.save(job_dir / "knowledge_graph.json")

    # Save job config
    config = {
        "job_id": job_id,
        "research_question": research_question,
        "data_files": [str(p) for p in data_paths],
        "max_iterations": max_iterations,
        "use_skills": use_skills,
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
    data_file = config["data_files"][0]  # Use first data file

    logger.info(f"Starting discovery for job {job_id}")

    # Track initial spend
    start_spend = get_cborg_spend()

    # Create job-specific MCP config
    mcp_config = {
        "mcpServers": {
            "shandy-tools": {
                "command": "python",
                "args": [
                    "-m", "shandy.mcp_server",
                    "--job-dir", str(job_dir.absolute()),
                    "--data-file", str(data_file)
                ]
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
        initial_prompt = f"""Begin autonomous discovery for this research question:

{config['research_question']}

You will run for a maximum of {max_iterations} iterations.

Data summary:
- Files: {config['data_files']}
- Columns: {kg.data['data_summary'].get('columns', [])}
- Samples: {kg.data['data_summary'].get('n_samples', 'Unknown')}

Start your investigation by exploring the data structure and searching literature.
"""

        logger.info(f"Starting discovery loop with Claude CLI headless mode")

        # Iteration 1: Start session
        cmd = [
            claude_cli,
            '-p', initial_prompt,
            '--output-format', 'json',
            '--mcp-config', str(mcp_config_path.absolute())
        ]

        logger.info(f"Iteration 1/{max_iterations}: Starting session")
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path.cwd()))

        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI failed: {result.stderr}")

        # Parse JSON output
        response_data = json.loads(result.stdout)
        session_id = response_data.get('session_id')

        if not session_id:
            raise RuntimeError("No session_id in Claude response")

        logger.info(f"Session started: {session_id}")

        # Log iteration
        log_file = job_dir / "claude_iterations.log"
        with open(log_file, "w") as f:
            f.write(f"=== Iteration 1 ===\n")
            f.write(f"Prompt: {initial_prompt}\n\n")
            f.write(f"Response: {json.dumps(response_data, indent=2)}\n\n")

        # Update knowledge graph iteration counter
        kg.increment_iteration()
        kg.save(job_dir / "knowledge_graph.json")

        # Iterations 2-N: Resume session
        for iteration in range(2, max_iterations + 1):
            # Reload knowledge graph to see latest state
            kg = KnowledgeGraph.load(job_dir / "knowledge_graph.json")

            # Build iteration prompt
            iteration_prompt = f"""# Iteration {iteration}/{max_iterations}

{kg.get_summary()}

---

Continue your investigation. Choose your next action:
- Explore data
- Search literature
- Test a hypothesis
- Record a finding

Think step by step about what will provide the most insight."""

            logger.info(f"Iteration {iteration}/{max_iterations}: Continuing session")

            # Resume session
            cmd = [
                claude_cli,
                '-p', iteration_prompt,
                '--resume', session_id,
                '--output-format', 'json'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path.cwd()))

            if result.returncode != 0:
                logger.error(f"Iteration {iteration} failed: {result.stderr}")
                break

            # Parse response
            response_data = json.loads(result.stdout)

            # Log iteration
            with open(log_file, "a") as f:
                f.write(f"=== Iteration {iteration} ===\n")
                f.write(f"Prompt: {iteration_prompt}\n\n")
                f.write(f"Response: {json.dumps(response_data, indent=2)}\n\n")

            # Update iteration counter
            kg.increment_iteration()
            kg.save(job_dir / "knowledge_graph.json")

            # Track costs
            try:
                current_cost = track_job_cost(job_id, start_spend, iteration, str(job_dir))
                logger.info(f"Current cost: ${current_cost:.2f}")
            except BudgetExceededError as e:
                logger.warning(f"Budget exceeded at iteration {iteration}: {e}")
                break

        logger.info(f"Discovery loop completed")

        # Generate final report using Claude
        logger.info("Generating final report...")
        kg = KnowledgeGraph.load(job_dir / "knowledge_graph.json")

        report_prompt = f"""You have completed autonomous discovery. Generate a final report summarizing your findings.

Research Question: {config['research_question']}

Knowledge Graph:
{json.dumps(kg.data, indent=2)}

Please create a comprehensive markdown report with:
1. Executive Summary (2-3 paragraphs)
2. Key Findings (with statistical evidence)
3. Mechanistic Model/Interpretation
4. Knowledge Gaps Identified
5. Proposed Follow-up Experiments

Format as professional scientific markdown."""

        # Generate report (single call, no session needed)
        cmd = [
            claude_cli,
            '-p', report_prompt,
            '--output-format', 'text'
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path.cwd()))

        if result.returncode == 0:
            report_content = result.stdout
            # Save report
            with open(job_dir / "final_report.md", "w") as f:
                f.write(report_content)
            logger.info("Final report generated")
        else:
            logger.error(f"Report generation failed: {result.stderr}")

        # Track final cost
        try:
            final_cost = track_job_cost(job_id, start_spend, max_iterations, str(job_dir))
        except BudgetExceededError as e:
            logger.warning(str(e))
            final_cost = None

        # Load final knowledge graph
        kg = KnowledgeGraph.load(job_dir / "knowledge_graph.json")

        # Update job status
        config["status"] = "completed"
        config["completed_at"] = datetime.now().isoformat()
        config["final_cost_usd"] = final_cost
        config["iterations_completed"] = kg.data["iteration"]
        config["findings_count"] = len(kg.data["findings"])

        with open(job_dir / "config.json", "w") as f:
            json.dump(config, f, indent=2)

        return {
            "job_id": job_id,
            "status": "completed",
            "iterations": kg.data["iteration"],
            "findings": len(kg.data["findings"]),
            "cost_usd": final_cost
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
    if result.get('cost_usd'):
        print(f"Cost: ${result['cost_usd']:.2f}")


if __name__ == "__main__":
    main()
