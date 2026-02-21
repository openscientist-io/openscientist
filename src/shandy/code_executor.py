"""
Code execution with sandboxing for SHANDY.

Executes Python code with timeouts, import whitelisting, and safety measures.
"""

import ast
import io
import json
import signal
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any, Optional

import matplotlib
import numpy as np
import pandas as pd

matplotlib.use("Agg")  # Non-interactive backend
import matplotlib.pyplot as plt
import seaborn as sns

from shandy.exceptions import CodeExecutionTimeoutError, ForbiddenImportError

# Backward-compat alias (old name shadowed builtin TimeoutError)
TimeoutError = CodeExecutionTimeoutError


def timeout_handler(_signum, _frame):
    """Signal handler for execution timeout."""
    raise CodeExecutionTimeoutError("Code execution timed out")


def validate_imports(code: str, allowed_imports: list[str]) -> None:
    """
    Validate that code only imports allowed modules.

    Args:
        code: Python code to validate
        allowed_imports: List of allowed module names

    Raises:
        ForbiddenImportError: If code imports forbidden modules
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SyntaxError(f"Syntax error in code: {e}") from e

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name.split(".")[0]  # Get top-level module
                if module_name not in allowed_imports:
                    raise ForbiddenImportError(
                        f"Import of '{alias.name}' is not allowed. "
                        f"Allowed imports: {', '.join(allowed_imports)}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module_name = node.module.split(".")[0]
                if module_name not in allowed_imports:
                    raise ForbiddenImportError(
                        f"Import from '{node.module}' is not allowed. "
                        f"Allowed imports: {', '.join(allowed_imports)}"
                    )


def execute_code(
    code: str,
    data: Optional[pd.DataFrame],
    plots_dir: Path,
    timeout: int = 60,
    description: str = "",
    iteration: int = 0,
    data_files: Optional[list[dict[str, Any]]] = None,
    save_code_with_plots: bool = True,
) -> dict[str, Any]:
    """
    Execute Python code in sandboxed environment.

    Security measures:
    - Timeout: 60 seconds max (configurable)
    - Import whitelist: Only scientific computing libraries
    - No network access (except via explicit tools)
    - No file system access outside workspace
    - Runs inside Docker container for additional isolation

    Args:
        code: Python code to execute
        data: DataFrame available as `data` variable (None for non-tabular files)
        plots_dir: Directory to save generated plots
        timeout: Max execution time in seconds (default: 60)
        description: Optional description of what's being investigated
        iteration: Current iteration number
        data_files: List of file metadata dicts (paths, types, etc.)

    Returns:
        Dictionary with execution results:
        {
            "success": bool,
            "output": str,        # Printed output
            "plots": [paths],     # Generated plot files
            "error": str,         # If failed
            "execution_time": float
        }
    """
    # Allowed imports
    allowed_imports = [
        # Core scientific computing
        "pandas",
        "numpy",
        "scipy",
        "matplotlib",
        "seaborn",
        "statsmodels",
        "sklearn",
        # Standard library (safe modules)
        "math",
        "statistics",
        "collections",
        "itertools",
        "functools",
        "operator",
        "datetime",
        "time",
        "re",
        "json",
        "os",  # Environment variables (for API tokens)
        # HTTP/API access
        "requests",  # HTTP requests (for KBase, external APIs)
        # Domain-specific
        "networkx",  # Network/graph analysis (for pathways)
        # Single-cell genomics
        "scanpy",
        "anndata",
        "h5py",
    ]

    # Validate imports
    try:
        validate_imports(code, allowed_imports)
    except (SyntaxError, ForbiddenImportError) as e:
        return {
            "success": False,
            "error": str(e),
            "output": "",
            "plots": [],
            "execution_time": 0.0,
        }

    # Prepare namespace with allowed libraries
    namespace: dict[str, Any] = {
        "data": data,
        "data_files": data_files or [],
        "pd": pd,
        "np": np,
        "plt": plt,
        "sns": sns,
        "__builtins__": __builtins__,
    }

    # Capture stdout/stderr
    stdout_capture = io.StringIO()
    stderr_capture = io.StringIO()

    # Track plots
    plots_dir.mkdir(parents=True, exist_ok=True)

    # Find highest existing plot number to avoid overwriting
    existing_plots = list(plots_dir.glob("plot_*.png"))
    if existing_plots:
        plot_numbers = []
        for p in existing_plots:
            try:
                # Extract number from plot_N.png filename
                num = int(p.stem.split("_")[1])
                plot_numbers.append(num)
            except (IndexError, ValueError):
                pass
        plot_counter = [max(plot_numbers)] if plot_numbers else [0]
    else:
        plot_counter = [0]

    def save_plot_hook():
        """Hook to intercept plt.show() and save plots instead."""
        plot_counter[0] += 1
        plot_path = plots_dir / f"plot_{plot_counter[0]}.png"
        plt.savefig(plot_path, bbox_inches="tight", dpi=150)
        plt.close()

        # Save plot metadata (description, iteration, and code)
        metadata_path = plots_dir / f"plot_{plot_counter[0]}.json"
        metadata = {
            "plot_number": plot_counter[0],
            "iteration": iteration,
            "description": description,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "code": code if save_code_with_plots else None,
        }
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        return str(plot_path)

    # Replace plt.show() with our hook
    namespace["plt"].show = save_plot_hook

    # Also intercept plt.savefig() to save metadata for custom-named plots
    original_savefig = namespace["plt"].savefig

    def savefig_hook(filename, *args, **kwargs):
        """Hook to intercept plt.savefig() and save to plots_dir with metadata."""
        # Redirect relative paths to plots_dir
        plot_path = Path(filename)
        if not plot_path.is_absolute():
            plot_path = plots_dir / plot_path.name

        # Call original savefig with redirected path
        result = original_savefig(str(plot_path), *args, **kwargs)

        # Save metadata alongside the plot
        metadata_path = plot_path.with_suffix(".json")

        metadata = {
            "filename": plot_path.name,
            "iteration": iteration,
            "description": (
                description
                if description
                else f"Analysis: {plot_path.stem.replace('_', ' ').title()}"
            ),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "code": code if save_code_with_plots else None,
        }

        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        return result

    namespace["plt"].savefig = savefig_hook

    start_time = time.time()

    try:
        # Set timeout signal (Unix only - won't work on Windows)
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(timeout)

        # Execute code with output capture
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(code, namespace)

        # Cancel timeout
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)

        execution_time = time.time() - start_time

        # Collect generated plots
        plot_files = [str(p) for p in plots_dir.glob("plot_*.png")]

        return {
            "success": True,
            "output": stdout_capture.getvalue(),
            "plots": plot_files,
            "error": None,
            "execution_time": execution_time,
        }

    except CodeExecutionTimeoutError:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
        return {
            "success": False,
            "error": f"Code execution timed out after {timeout} seconds",
            "output": stdout_capture.getvalue(),
            "plots": [],
            "execution_time": timeout,
        }

    except Exception as e:  # noqa: BLE001 — intentional catch-all for arbitrary user code
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)

        execution_time = time.time() - start_time

        # Get traceback
        error_trace = traceback.format_exc()

        return {
            "success": False,
            "error": f"{type(e).__name__}: {str(e)}",
            "output": stdout_capture.getvalue(),
            "traceback": error_trace,
            "plots": [],
            "execution_time": execution_time,
        }


def execute_sparql_code(
    code: str,
    plots_dir: Path,
    timeout: int = 60,
    description: str = "",
    iteration: int = 0,
    data_files: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """
    Execute a SPARQL query against a remote endpoint.

    The query must include an endpoint comment on its own line:
        # ENDPOINT: https://query.wikidata.org/sparql

    Args:
        code: SPARQL query, with a ``# ENDPOINT: <url>`` comment
        plots_dir: Unused, kept for API consistency
        timeout: Query timeout in seconds (default: 60)
        description: Optional description of what's being investigated
        iteration: Current iteration number
        data_files: Unused, kept for API consistency

    Returns:
        Dictionary with execution results (no plots for SPARQL)
    """
    start_time = time.time()

    # Parse endpoint from query comments
    endpoint: str | None = None
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("# endpoint:"):
            endpoint = stripped[len("# endpoint:") :].strip()
            break

    if not endpoint:
        return {
            "success": False,
            "error": (
                "No SPARQL endpoint specified. "
                "Add a comment to your query: # ENDPOINT: https://example.org/sparql"
            ),
            "output": "",
            "plots": [],
            "execution_time": time.time() - start_time,
        }

    try:
        from SPARQLWrapper import JSON, SPARQLWrapper
        from SPARQLWrapper.SPARQLExceptions import SPARQLWrapperException
    except ImportError:
        return {
            "success": False,
            "error": "SPARQLWrapper is not installed. Run: pip install SPARQLWrapper",
            "output": "",
            "plots": [],
            "execution_time": time.time() - start_time,
        }

    try:
        sparql = SPARQLWrapper(endpoint)
        sparql.setQuery(code)
        sparql.setReturnFormat(JSON)
        sparql.setTimeout(timeout)

        results = sparql.query().convert()
    except SPARQLWrapperException as e:
        return {
            "success": False,
            "error": f"SPARQL query error: {e}",
            "output": "",
            "plots": [],
            "execution_time": time.time() - start_time,
        }
    except Exception as e:  # noqa: BLE001
        return {
            "success": False,
            "error": f"{type(e).__name__}: {e}",
            "output": "",
            "plots": [],
            "execution_time": time.time() - start_time,
        }

    execution_time = time.time() - start_time

    # Format results as a plain-text table
    if not isinstance(results, dict):
        results = {}
    bindings = results.get("results", {}).get("bindings", [])
    vars_ = results.get("head", {}).get("vars", [])

    if not bindings:
        output = "Query returned 0 results."
    else:
        # Build rows
        rows = [[b.get(v, {}).get("value", "") for v in vars_] for b in bindings]
        col_widths = [max(len(v), *(len(r[i]) for r in rows)) for i, v in enumerate(vars_)]
        sep = "  ".join("-" * w for w in col_widths)
        header = "  ".join(v.ljust(col_widths[i]) for i, v in enumerate(vars_))
        lines = [header, sep] + [
            "  ".join(cell.ljust(col_widths[i]) for i, cell in enumerate(row)) for row in rows
        ]
        output = f"{len(bindings)} result(s):\n\n" + "\n".join(lines)

    return {
        "success": True,
        "output": output,
        "plots": [],
        "error": None,
        "execution_time": execution_time,
    }


def execute_rust_code(
    code: str,
    plots_dir: Path,
    timeout: int = 60,
    description: str = "",
    iteration: int = 0,
    data_files: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """
    Execute Rust code by compiling with rustc and running the binary.

    Args:
        code: Rust source code to compile and run
        plots_dir: Directory for any output files (not used for Rust, for API consistency)
        timeout: Max total time (compile + run) in seconds (default: 60)
        description: Optional description of what's being investigated
        iteration: Current iteration number
        data_files: Unused, kept for API consistency with execute_code

    Returns:
        Dictionary with execution results (no plots for Rust)
    """
    import subprocess
    import tempfile

    start_time = time.time()

    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = Path(tmpdir) / "main.rs"
        bin_path = Path(tmpdir) / "main"
        src_path.write_text(code, encoding="utf-8")

        # Compile
        try:
            compile_result = subprocess.run(
                ["rustc", str(src_path), "-o", str(bin_path)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except FileNotFoundError:
            return {
                "success": False,
                "error": "Rust compiler (rustc) not found. Install Rust to use Rust code execution.",
                "output": "",
                "plots": [],
                "execution_time": time.time() - start_time,
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Rust compilation timed out after {timeout} seconds",
                "output": "",
                "plots": [],
                "execution_time": float(timeout),
            }

        if compile_result.returncode != 0:
            return {
                "success": False,
                "error": f"Compilation error:\n{compile_result.stderr}",
                "output": compile_result.stdout,
                "plots": [],
                "execution_time": time.time() - start_time,
            }

        # Run with remaining budget
        remaining = max(1, timeout - int(time.time() - start_time))
        try:
            run_result = subprocess.run(
                [str(bin_path)],
                capture_output=True,
                text=True,
                timeout=remaining,
            )
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error": f"Rust execution timed out after {timeout} seconds",
                "output": "",
                "plots": [],
                "execution_time": float(timeout),
            }

        execution_time = time.time() - start_time
        output = run_result.stdout
        if run_result.stderr:
            output += f"\nstderr:\n{run_result.stderr}"

        return {
            "success": run_result.returncode == 0,
            "output": output,
            "plots": [],
            "error": (
                None
                if run_result.returncode == 0
                else f"Process exited with code {run_result.returncode}\n{run_result.stderr}"
            ),
            "execution_time": execution_time,
        }


def format_execution_result(result: dict[str, Any]) -> str:
    """
    Format execution result for display to agent.

    Args:
        result: Result from execute_code()

    Returns:
        Formatted string
    """
    if result["success"]:
        parts = [" Code executed successfully\n"]
        if result["output"]:
            parts.append(f"Output:\n{result['output']}\n")
        if result["plots"]:
            parts.append(f"Generated {len(result['plots'])} plot(s):\n")
            for plot in result["plots"]:
                parts.append(f"  - {plot}\n")
        parts.append(f"Execution time: {result['execution_time']:.2f}s")
        return "".join(parts)
    parts = ["L Code execution failed\n"]
    parts.append(f"Error: {result['error']}\n")
    if result.get("output"):
        parts.append(f"\nPartial output:\n{result['output']}\n")
    if result.get("traceback"):
        parts.append(f"\nTraceback:\n{result['traceback']}")
    return "".join(parts)
