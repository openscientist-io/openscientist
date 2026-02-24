"""
Phenix structural biology tools for the SDK agent path.

Only imported when PHENIX_PATH is configured.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Callable
from pathlib import Path

from shandy.tools.registry import ToolContext, tool

logger = logging.getLogger(__name__)


def _resolve_phenix_file_paths(
    ctx: ToolContext, input_files: list[str]
) -> tuple[list[str], str | None]:
    """Resolve input filenames under the job's data directory."""
    data_dir = ctx.job_dir / "data"
    resolved_files: list[str] = []
    for filename in input_files:
        file_path = data_dir / filename
        if not file_path.exists():
            return [], f"❌ Error: File not found: {filename}"
        resolved_files.append(str(file_path))
    return resolved_files, None


def _build_phenix_command(
    tool_name: str, resolved_files: list[str], arguments: dict[str, object] | None
) -> list[str]:
    """Build Phenix CLI command line."""
    cmd = [tool_name, *resolved_files]
    if arguments:
        for key, value in arguments.items():
            cmd.append(f"{key}={value}")
    return cmd


def _format_phenix_output(
    tool_name: str,
    input_files: list[str],
    description: str,
    result: subprocess.CompletedProcess[str],
) -> str:
    """Format CLI execution output for the agent."""
    output_parts: list[str] = []
    if description:
        output_parts.append(f"=== {description} ===\n")
    output_parts.append(f"Command: {' '.join([tool_name, *input_files])}\n")
    output_parts.append(f"\n{result.stdout}")
    if result.stderr:
        output_parts.append(f"\n⚠️  Errors/Warnings:\n{result.stderr}")
    if result.returncode != 0:
        output_parts.append(f"\n❌ Tool exited with code {result.returncode}")
    return "".join(output_parts)


def _log_phenix_execution(
    ctx: ToolContext,
    tool_name: str,
    input_files: list[str],
    description: str,
    success: bool,
) -> None:
    """Record Phenix tool execution in knowledge state."""
    from shandy.knowledge_state import KnowledgeState

    ks = KnowledgeState.load(ctx.job_dir / "knowledge_state.json")
    ks.log_analysis(
        action="run_phenix_tool",
        tool_name=tool_name,
        input_files=input_files,
        description=description,
        success=success,
    )
    ks.save(ctx.job_dir / "knowledge_state.json")


def _run_phenix_tool_impl(
    ctx: ToolContext,
    tool_name: str,
    input_files: list[str],
    arguments: dict[str, object] | None = None,
    description: str = "",
) -> str:
    """Execute a Phenix command-line tool."""
    from shandy.phenix_setup import setup_phenix_env

    phenix_env = setup_phenix_env()
    if not phenix_env:
        return "❌ Error: PHENIX_PATH not configured."

    resolved_files, err = _resolve_phenix_file_paths(ctx, input_files)
    if err:
        return err

    cmd = _build_phenix_command(tool_name, resolved_files, arguments)
    try:
        result = subprocess.run(
            cmd,
            env=phenix_env,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return f"❌ Error: {tool_name} timed out after 5 minutes"
    except FileNotFoundError:
        return f"❌ Error: Tool '{tool_name}' not found. Check PHENIX_PATH."
    except (OSError, subprocess.SubprocessError) as e:
        return f"❌ Error running {tool_name}: {e}"

    _log_phenix_execution(
        ctx=ctx,
        tool_name=tool_name,
        input_files=input_files,
        description=description,
        success=(result.returncode == 0),
    )
    return _format_phenix_output(tool_name, input_files, description, result)


def _compare_structures_impl(
    ctx: ToolContext, experimental_pdb: str, predicted_pdb: str, description: str = ""
) -> str:
    """Compare experimental and predicted structures using phenix.superpose_pdbs."""
    desc = description or "Comparing experimental and predicted structures"
    result = _run_phenix_tool_impl(
        ctx=ctx,
        tool_name="phenix.superpose_pdbs",
        input_files=[experimental_pdb, predicted_pdb],
        description=desc,
    )
    if "RMSD" in result or "rms" in result.lower():
        result += "\n\n💡 Interpretation hints:"
        result += "\n  - RMSD < 1.0 Å: Excellent agreement"
        result += "\n  - RMSD 1-2 Å: Good agreement, minor differences"
        result += "\n  - RMSD 2-4 Å: Moderate differences"
        result += "\n  - RMSD > 4 Å: Significant differences"
    return result


def _extract_ca_confidence(pdb_path: Path) -> tuple[list[int], list[float]]:
    """Extract residue numbers and CA atom pLDDT values from a PDB file."""
    residues: list[int] = []
    plddts: list[float] = []
    with open(pdb_path, encoding="utf-8") as file_obj:
        for line in file_obj:
            if line.startswith("ATOM") and line[12:16].strip() == "CA":
                residues.append(int(line[22:26].strip()))
                plddts.append(float(line[60:66].strip()))
    return residues, plddts


def _find_low_confidence_regions(
    residues: list[int], plddts: list[float], threshold: float = 70.0
) -> list[str]:
    """Return residue ranges where confidence drops below threshold."""
    regions: list[str] = []
    region_start: int | None = None
    previous_residue: int | None = None

    for residue, plddt in zip(residues, plddts, strict=False):
        if plddt < threshold:
            if region_start is None:
                region_start = residue
            previous_residue = residue
            continue
        if region_start is not None and previous_residue is not None:
            regions.append(f"{region_start}-{previous_residue}")
            region_start = None
            previous_residue = None

    if region_start is not None and previous_residue is not None:
        regions.append(f"{region_start}-{previous_residue}")
    return regions


def _load_pae_entry_count(pae_path: Path) -> int | None:
    """Return PAE payload size when JSON exists and parses."""
    if not pae_path.exists():
        return None
    with open(pae_path, encoding="utf-8") as file_obj:
        pae_data = json.load(file_obj)
    if isinstance(pae_data, dict):
        return len(pae_data)
    if isinstance(pae_data, list):
        return len(pae_data)
    return 1


def _parse_alphafold_confidence_impl(
    ctx: ToolContext, alphafold_pdb: str, pae_json: str | None = None
) -> str:
    """Extract AlphaFold confidence metrics (pLDDT) from a PDB file."""
    data_dir = ctx.job_dir / "data"
    pdb_path = data_dir / alphafold_pdb

    if not pdb_path.exists():
        return f"❌ Error: File not found: {alphafold_pdb}"

    try:
        residues, plddts = _extract_ca_confidence(pdb_path)
    except (OSError, ValueError) as e:
        return f"❌ Error parsing AlphaFold file: {e}"

    if not plddts:
        return "❌ Error: No CA atoms found in PDB file"

    avg_plddt = sum(plddts) / len(plddts)
    min_plddt = min(plddts)
    max_plddt = max(plddts)
    low_conf_regions = _find_low_confidence_regions(residues, plddts)

    pct_high = sum(1 for value in plddts if value >= 90) / len(plddts) * 100
    pct_confident = sum(1 for value in plddts if value >= 70) / len(plddts) * 100

    output = [
        f"AlphaFold Confidence Analysis: {alphafold_pdb}",
        f"\nResidues analyzed: {len(plddts)}",
        f"Average pLDDT: {avg_plddt:.1f}",
        f"Min pLDDT: {min_plddt:.1f}",
        f"Max pLDDT: {max_plddt:.1f}",
        f"\nHigh confidence (>90): {pct_high:.1f}%",
        f"Confident (>70): {pct_confident:.1f}%",
    ]

    if low_conf_regions:
        output.append(f"\nLow confidence regions (<70): {', '.join(low_conf_regions)}")
    else:
        output.append("\n✅ No low confidence regions detected")

    if pae_json:
        try:
            pae_count = _load_pae_entry_count(data_dir / pae_json)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            return f"❌ Error parsing AlphaFold file: {e}"
        if pae_count is not None:
            output.append(f"\nPAE data loaded: {pae_count} entries")

    return "\n".join(output)


def make_tools(ctx: ToolContext) -> list[Callable]:
    """Return Phenix tools bound to ctx. Empty list if Phenix unavailable."""
    from shandy.phenix_setup import check_phenix_available

    if not check_phenix_available():
        return []

    @tool
    def run_phenix_tool(
        tool_name: str,
        input_files: list[str],
        arguments: dict | None = None,
        description: str = "",
    ) -> str:
        """
        Execute a Phenix command-line tool.

        Available tools include:
        - phenix.superpose_pdbs: Align and compare two structures
        - phenix.clashscore: Detect steric clashes
        - phenix.cablam_validate: Validate backbone geometry

        Args:
            tool_name: Name of Phenix tool (e.g., "phenix.clashscore")
            input_files: List of PDB/mmCIF file paths (relative to job data directory)
            arguments: Optional dict of command-line arguments
            description: What you're investigating

        Returns:
            Tool output (stdout/stderr, parsed results if available)
        """
        return _run_phenix_tool_impl(
            ctx=ctx,
            tool_name=tool_name,
            input_files=input_files,
            arguments=arguments,
            description=description,
        )

    @tool
    def compare_structures(experimental_pdb: str, predicted_pdb: str, description: str = "") -> str:
        """
        Compare experimental and predicted protein structures using Phenix.

        Args:
            experimental_pdb: Experimental structure file (relative to job data dir)
            predicted_pdb: Predicted structure file (relative to job data dir)
            description: What you're investigating

        Returns:
            Alignment results with RMSD values and statistics
        """
        return _compare_structures_impl(
            ctx=ctx,
            experimental_pdb=experimental_pdb,
            predicted_pdb=predicted_pdb,
            description=description,
        )

    @tool
    def parse_alphafold_confidence(alphafold_pdb: str, pae_json: str | None = None) -> str:
        """
        Extract AlphaFold confidence metrics (pLDDT) from a PDB file.

        Args:
            alphafold_pdb: AlphaFold PDB file (relative to job data dir)
            pae_json: Optional PAE JSON file

        Returns:
            Per-residue confidence scores and summary statistics
        """
        return _parse_alphafold_confidence_impl(
            ctx=ctx,
            alphafold_pdb=alphafold_pdb,
            pae_json=pae_json,
        )

    return [run_phenix_tool, compare_structures, parse_alphafold_confidence]
