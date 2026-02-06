"""Phenix-specific MCP tools for structural biology analysis."""

import json
import subprocess
from pathlib import Path

from ..phenix_setup import setup_phenix_env


def register_phenix_tools(mcp, job_dir: Path, ks):
    """
    Register Phenix MCP tools with the server.

    Args:
        mcp: FastMCP server instance
        job_dir: Job directory path
        ks: Knowledge state instance
    """

    @mcp.tool()
    def run_phenix_tool(
        tool_name: str,
        input_files: list[str],
        arguments: dict = None,  # type: ignore[assignment]
        description: str = "",
    ) -> str:
        """
        Execute a Phenix command-line tool.

        Available tools include:
        - phenix.superpose_pdbs: Align and compare two structures
        - phenix.clashscore: Detect steric clashes
        - phenix.cablam_validate: Validate backbone geometry
        - phenix.chain_comparison: Compare protein chains

        Args:
            tool_name: Name of Phenix tool (e.g., "phenix.clashscore")
            input_files: List of PDB/mmCIF file paths (relative to job data directory)
            arguments: Optional dict of command-line arguments (e.g., {"nproc": "4"})
            description: What you're investigating (shown in logs and output)

        Returns:
            Tool output (stdout/stderr, parsed results if available)
        """
        # Get Phenix environment
        phenix_env = setup_phenix_env()
        if not phenix_env:
            return "❌ Error: PHENIX_PATH not configured. Set PHENIX_PATH in .env to enable Phenix tools."

        # Resolve input file paths
        data_dir = job_dir / "data"
        resolved_files = []
        for f in input_files:
            file_path = data_dir / f
            if not file_path.exists():
                return f"❌ Error: File not found: {f}"
            resolved_files.append(str(file_path))

        # Build command
        cmd = [tool_name] + resolved_files
        if arguments:
            for key, val in arguments.items():
                cmd.append(f"{key}={val}")

        # Execute with timeout
        try:
            result = subprocess.run(
                cmd,
                env=phenix_env,
                capture_output=True,
                text=True,
                timeout=300,  # 5 min timeout
            )

            # Format output
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

            # Log to knowledge graph
            ks.log_analysis(
                action="run_phenix_tool",
                tool_name=tool_name,
                input_files=input_files,
                description=description,
                success=(result.returncode == 0),
            )
            ks.save(job_dir / "knowledge_state.json")

            return output

        except subprocess.TimeoutExpired:
            return f"❌ Error: {tool_name} timed out after 5 minutes"
        except FileNotFoundError:
            return f"❌ Error: Tool '{tool_name}' not found. Check PHENIX_PATH is correct."
        except Exception as e:
            return f"❌ Error running {tool_name}: {str(e)}"

    @mcp.tool()
    def compare_structures(experimental_pdb: str, predicted_pdb: str, description: str = "") -> str:
        """
        Compare experimental and predicted protein structures.

        Uses phenix.superpose_pdbs to align structures and calculate RMSD.
        Returns global RMSD and alignment information.

        Args:
            experimental_pdb: Experimental structure file (e.g., "3ts8.pdb")
            predicted_pdb: Predicted structure file (e.g., "alphafold_P04637.pdb")
            description: What you're investigating (optional)

        Returns:
            Alignment results with RMSD values and statistics
        """
        desc = description or "Comparing experimental and predicted structures"

        # Use run_phenix_tool to execute superpose_pdbs
        result = run_phenix_tool(
            tool_name="phenix.superpose_pdbs",
            input_files=[experimental_pdb, predicted_pdb],
            description=desc,
        )

        # Add interpretation hints
        if "RMSD" in result or "rms" in result.lower():
            result += "\n\n💡 Interpretation hints:"
            result += "\n  - RMSD < 1.0 Å: Excellent agreement"
            result += "\n  - RMSD 1-2 Å: Good agreement, minor differences"
            result += "\n  - RMSD 2-4 Å: Moderate differences, investigate regions"
            result += "\n  - RMSD > 4 Å: Significant differences, likely different conformations"

        return result  # type: ignore[no-any-return]

    @mcp.tool()
    def parse_alphafold_confidence(alphafold_pdb: str, pae_json: str = None) -> str:  # type: ignore[assignment]
        """
        Extract AlphaFold confidence metrics from prediction files.

        AlphaFold stores per-residue confidence (pLDDT) in the B-factor column of PDB files.
        Optionally can also parse PAE (Predicted Aligned Error) JSON file.

        Args:
            alphafold_pdb: AlphaFold PDB file (e.g., "alphafold_P04637.pdb")
            pae_json: Optional PAE JSON file (e.g., "alphafold_P04637_pae.json")

        Returns:
            Per-residue confidence scores and summary statistics
        """
        data_dir = job_dir / "data"
        pdb_path = data_dir / alphafold_pdb

        if not pdb_path.exists():
            return f"❌ Error: File not found: {alphafold_pdb}"

        try:
            # Parse PDB file to extract pLDDT from B-factor column
            residues = []
            plddts = []

            with open(pdb_path, "r") as f:
                for line in f:
                    # Look for CA atoms (one per residue)
                    if line.startswith("ATOM") and line[12:16].strip() == "CA":
                        res_num = int(line[22:26].strip())
                        plddt = float(line[60:66].strip())
                        residues.append(res_num)
                        plddts.append(plddt)

            if not plddts:
                return "❌ Error: No CA atoms found in PDB file"

            # Calculate statistics
            avg_plddt = sum(plddts) / len(plddts)
            min_plddt = min(plddts)
            max_plddt = max(plddts)

            # Find low confidence regions (pLDDT < 70)
            low_conf_regions = []
            in_region = False
            region_start = None

            for res, plddt in zip(residues, plddts):
                if plddt < 70 and not in_region:
                    region_start = res
                    in_region = True
                elif plddt >= 70 and in_region:
                    low_conf_regions.append(f"{region_start}-{residues[residues.index(res) - 1]}")
                    in_region = False

            if in_region:  # Close last region
                low_conf_regions.append(f"{region_start}-{residues[-1]}")

            # Format output
            output = "=== AlphaFold Confidence Analysis ===\n\n"
            output += f"File: {alphafold_pdb}\n"
            output += f"Residues analyzed: {len(residues)}\n\n"
            output += "📊 pLDDT Statistics:\n"
            output += f"  - Average: {avg_plddt:.2f}\n"
            output += f"  - Range: {min_plddt:.2f} - {max_plddt:.2f}\n\n"

            if low_conf_regions:
                output += "⚠️  Low confidence regions (pLDDT < 70):\n"
                for region in low_conf_regions:
                    output += f"  - Residues {region}\n"
            else:
                output += "✅ No low confidence regions found (all pLDDT ≥ 70)\n"

            output += "\n💡 pLDDT Interpretation:\n"
            output += "  - pLDDT > 90: Very high confidence\n"
            output += "  - pLDDT 70-90: Confident\n"
            output += "  - pLDDT 50-70: Low confidence (often flexible regions)\n"
            output += "  - pLDDT < 50: Very low confidence (disordered)\n"

            # Parse PAE if provided
            if pae_json:
                pae_path = data_dir / pae_json
                if pae_path.exists():
                    with open(pae_path, "r") as f:
                        _pae_data = json.load(f)
                    output += f"\n\n📈 PAE data loaded from {pae_json}"
                    output += "\n(Use execute_code to visualize PAE matrix)"

            # Log to knowledge graph
            ks.log_analysis(
                action="parse_alphafold_confidence",
                file=alphafold_pdb,
                avg_plddt=avg_plddt,
                low_conf_regions=low_conf_regions,
            )
            ks.save(job_dir / "knowledge_state.json")

            return output

        except Exception as e:
            return f"❌ Error parsing AlphaFold confidence: {str(e)}"
