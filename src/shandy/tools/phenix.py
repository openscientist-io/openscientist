"""
Phenix structural biology tools for the SDK agent path.

Only imported when PHENIX_PATH is configured.
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from typing import Optional

from shandy.tools.registry import ToolContext, tool

logger = logging.getLogger(__name__)


def make_tools(ctx: ToolContext) -> list[Callable]:
    """Return Phenix tools bound to ctx. Empty list if Phenix unavailable."""
    from shandy.phenix_setup import check_phenix_available

    if not check_phenix_available():
        return []

    @tool
    def run_phenix_tool(
        tool_name: str,
        input_files: list[str],
        arguments: Optional[dict] = None,
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
        from shandy.knowledge_state import KnowledgeState
        from shandy.phenix_setup import setup_phenix_env

        phenix_env = setup_phenix_env()
        if not phenix_env:
            return "❌ Error: PHENIX_PATH not configured."

        data_dir = ctx.job_dir / "data"
        resolved_files = []
        for f in input_files:
            file_path = data_dir / f
            if not file_path.exists():
                return f"❌ Error: File not found: {f}"
            resolved_files.append(str(file_path))

        cmd = [tool_name] + resolved_files
        if arguments:
            for key, val in arguments.items():
                cmd.append(f"{key}={val}")

        try:
            result = subprocess.run(
                cmd,
                env=phenix_env,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )

            output_parts = []
            if description:
                output_parts.append(f"=== {description} ===\n")
            output_parts.append(f"Command: {' '.join([tool_name] + input_files)}\n")
            output_parts.append(f"\n{result.stdout}")
            if result.stderr:
                output_parts.append(f"\n⚠️  Errors/Warnings:\n{result.stderr}")
            if result.returncode != 0:
                output_parts.append(f"\n❌ Tool exited with code {result.returncode}")

            output = "".join(output_parts)

            ks = KnowledgeState.load(ctx.job_dir / "knowledge_state.json")
            ks.log_analysis(
                action="run_phenix_tool",
                tool_name=tool_name,
                input_files=input_files,
                description=description,
                success=(result.returncode == 0),
            )
            ks.save(ctx.job_dir / "knowledge_state.json")
            return output

        except subprocess.TimeoutExpired:
            return f"❌ Error: {tool_name} timed out after 5 minutes"
        except FileNotFoundError:
            return f"❌ Error: Tool '{tool_name}' not found. Check PHENIX_PATH."
        except (OSError, subprocess.SubprocessError) as e:
            return f"❌ Error running {tool_name}: {e}"

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
        desc = description or "Comparing experimental and predicted structures"
        result = run_phenix_tool(
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
        return str(result)

    @tool
    def parse_alphafold_confidence(alphafold_pdb: str, pae_json: Optional[str] = None) -> str:
        """
        Extract AlphaFold confidence metrics (pLDDT) from a PDB file.

        Args:
            alphafold_pdb: AlphaFold PDB file (relative to job data dir)
            pae_json: Optional PAE JSON file

        Returns:
            Per-residue confidence scores and summary statistics
        """
        data_dir = ctx.job_dir / "data"
        pdb_path = data_dir / alphafold_pdb

        if not pdb_path.exists():
            return f"❌ Error: File not found: {alphafold_pdb}"

        try:
            residues = []
            plddts = []
            with open(pdb_path, encoding="utf-8") as f:
                for line in f:
                    if line.startswith("ATOM") and line[12:16].strip() == "CA":
                        residues.append(int(line[22:26].strip()))
                        plddts.append(float(line[60:66].strip()))

            if not plddts:
                return "❌ Error: No CA atoms found in PDB file"

            avg_plddt = sum(plddts) / len(plddts)
            min_plddt = min(plddts)
            max_plddt = max(plddts)

            # Find low confidence regions
            low_conf_regions: list[str] = []
            in_region = False
            region_start: Optional[int] = None
            for res, plddt in zip(residues, plddts):
                if plddt < 70 and not in_region:
                    region_start = res
                    in_region = True
                elif plddt >= 70 and in_region:
                    idx = residues.index(res)
                    low_conf_regions.append(f"{region_start}-{residues[idx - 1]}")
                    in_region = False
            if in_region and region_start is not None:
                low_conf_regions.append(f"{region_start}-{residues[-1]}")

            pct_high = sum(1 for p in plddts if p >= 90) / len(plddts) * 100
            pct_confident = sum(1 for p in plddts if p >= 70) / len(plddts) * 100

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

            # Parse PAE if provided
            if pae_json:
                import json

                pae_path = data_dir / pae_json
                if pae_path.exists():
                    with open(pae_path, encoding="utf-8") as f:
                        pae_data = json.load(f)
                    output.append(f"\nPAE data loaded: {len(pae_data)} entries")

            return "\n".join(output)

        except (OSError, ValueError) as e:
            return f"❌ Error parsing AlphaFold file: {e}"

    return [run_phenix_tool, compare_structures, parse_alphafold_confidence]
