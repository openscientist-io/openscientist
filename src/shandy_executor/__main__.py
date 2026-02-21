"""
SHANDY Executor main entry point.

Reads JSON from stdin with:
- code: Code to execute
- language: Programming language ("python" or "rust", default: "python")
- data_path: Optional path to data file (CSV, Parquet, etc.)
- output_dir: Directory to save plots and artifacts
- timeout: Max execution time in seconds (default: 60)

Writes JSON to stdout with:
- success: bool
- output: str (printed output)
- plots: list of plot file paths
- error: str (if failed)
- execution_time: float
"""

import io
import json
import signal
import sys
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")  # Non-interactive backend

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import seaborn as sns


class ExecutionTimeoutError(Exception):
    """Raised when code execution times out."""

    pass


def timeout_handler(_signum, _frame):
    """Signal handler for execution timeout."""
    raise ExecutionTimeoutError("Code execution timed out")


def load_data(data_path: str | None) -> pd.DataFrame | None:
    """Load data from file path."""
    if not data_path:
        return None

    path = Path(data_path)
    if not path.exists():
        raise FileNotFoundError(f"Data file not found: {data_path}")

    suffix = path.suffix.lower()

    if suffix == ".csv":
        return pd.read_csv(path)
    elif suffix == ".tsv":
        return pd.read_csv(path, sep="\t")
    elif suffix == ".parquet":
        return pd.read_parquet(path)
    elif suffix in (".xlsx", ".xls"):
        return pd.read_excel(path)
    elif suffix == ".json":
        return pd.read_json(path)
    else:
        # Try CSV as fallback
        return pd.read_csv(path)


def execute_code(
    code: str,
    data: pd.DataFrame | None,
    output_dir: Path,
    timeout: int = 60,
    description: str = "",
    iteration: int = 0,
    data_files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Execute Python code in sandboxed environment.

    Args:
        code: Python code to execute
        data: DataFrame available as `data` variable (None for non-tabular files)
        output_dir: Directory to save generated plots
        timeout: Max execution time in seconds (default: 60)
        description: Optional description of what's being investigated
        iteration: Current iteration number
        data_files: List of file metadata dicts (paths, types, etc.)

    Returns:
        Dictionary with execution results
    """
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
    output_dir.mkdir(parents=True, exist_ok=True)

    # Find highest existing plot number to avoid overwriting
    existing_plots = list(output_dir.glob("plot_*.png"))
    if existing_plots:
        plot_numbers = []
        for p in existing_plots:
            try:
                num = int(p.stem.split("_")[1])
                plot_numbers.append(num)
            except (IndexError, ValueError):
                pass
        plot_counter = [max(plot_numbers)] if plot_numbers else [0]
    else:
        plot_counter = [0]

    generated_plots: list[str] = []

    def save_plot_hook():
        """Hook to intercept plt.show() and save plots instead."""
        plot_counter[0] += 1
        plot_path = output_dir / f"plot_{plot_counter[0]}.png"
        plt.savefig(plot_path, bbox_inches="tight", dpi=150)
        plt.close()

        # Save plot metadata
        metadata_path = output_dir / f"plot_{plot_counter[0]}.json"
        metadata = {
            "plot_number": plot_counter[0],
            "iteration": iteration,
            "description": description,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "code": code,
        }
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        generated_plots.append(str(plot_path))
        return str(plot_path)

    # Replace plt.show() with our hook
    namespace["plt"].show = save_plot_hook

    # Also intercept plt.savefig()
    original_savefig = namespace["plt"].savefig

    def savefig_hook(filename, *args, **kwargs):
        """Hook to intercept plt.savefig() and save to output_dir with metadata."""
        plot_path = Path(filename)
        if not plot_path.is_absolute():
            plot_path = output_dir / plot_path.name

        result = original_savefig(str(plot_path), *args, **kwargs)

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
            "code": code,
        }
        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)

        generated_plots.append(str(plot_path))
        return result

    namespace["plt"].savefig = savefig_hook

    start_time = time.time()

    try:
        # Set timeout signal
        if hasattr(signal, "SIGALRM"):
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(timeout)

        # Execute code with output capture
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(code, namespace)  # noqa: S102 — intentional user code execution

        # Cancel timeout
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)

        execution_time = time.time() - start_time

        return {
            "success": True,
            "output": stdout_capture.getvalue(),
            "plots": generated_plots,
            "error": None,
            "execution_time": execution_time,
        }

    except ExecutionTimeoutError:
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)
        return {
            "success": False,
            "error": f"Code execution timed out after {timeout} seconds",
            "output": stdout_capture.getvalue(),
            "plots": [],
            "execution_time": timeout,
        }

    except Exception as e:  # noqa: BLE001 — intentional catch-all for user code
        if hasattr(signal, "SIGALRM"):
            signal.alarm(0)

        execution_time = time.time() - start_time
        error_trace = traceback.format_exc()

        return {
            "success": False,
            "error": f"{type(e).__name__}: {e!s}",
            "output": stdout_capture.getvalue(),
            "traceback": error_trace,
            "plots": [],
            "execution_time": execution_time,
        }


def execute_sparql_code(
    code: str,
    output_dir: Path,
    timeout: int = 60,
    description: str = "",
    iteration: int = 0,
    data_files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Execute a SPARQL query against a remote endpoint.

    The query must include an endpoint comment:
        # ENDPOINT: https://query.wikidata.org/sparql
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
            "error": "SPARQLWrapper is not installed.",
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

    if not isinstance(results, dict):
        results = {}
    bindings = results.get("results", {}).get("bindings", [])
    vars_ = results.get("head", {}).get("vars", [])

    if not bindings:
        output = "Query returned 0 results."
    else:
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
    output_dir: Path,
    timeout: int = 60,
    description: str = "",
    iteration: int = 0,
    data_files: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """
    Execute Rust code by compiling with rustc and running the binary.

    Args:
        code: Rust source code to compile and run
        output_dir: Directory for output files (unused for Rust, for API consistency)
        timeout: Max total time (compile + run) in seconds
        description: Optional description of what's being investigated
        iteration: Current iteration number
        data_files: Unused, kept for API consistency

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
                check=False,
            )
        except FileNotFoundError:
            return {
                "success": False,
                "error": "Rust compiler (rustc) not found.",
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
                check=False,
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


def main():
    """Main entry point - reads from stdin, writes to stdout."""
    try:
        # Read JSON input from stdin
        input_data = json.load(sys.stdin)

        code = input_data.get("code", "")
        language = input_data.get("language", "python")
        data_path = input_data.get("data_path")
        output_dir = Path(input_data.get("output_dir", "/output"))
        timeout = input_data.get("timeout", 60)
        description = input_data.get("description", "")
        iteration = input_data.get("iteration", 0)
        data_files = input_data.get("data_files", [])

        if language == "rust":
            result = execute_rust_code(
                code=code,
                output_dir=output_dir,
                timeout=timeout,
                description=description,
                iteration=iteration,
                data_files=data_files,
            )
        elif language == "sparql":
            result = execute_sparql_code(
                code=code,
                output_dir=output_dir,
                timeout=timeout,
                description=description,
                iteration=iteration,
                data_files=data_files,
            )
        else:
            # Load data if path provided (only relevant for Python)
            data = load_data(data_path)

            # Execute code
            result = execute_code(
                code=code,
                data=data,
                output_dir=output_dir,
                timeout=timeout,
                description=description,
                iteration=iteration,
                data_files=data_files,
            )

        # Write result to stdout
        json.dump(result, sys.stdout)
        sys.stdout.flush()

    except Exception as e:  # noqa: BLE001 — top-level safety net
        error_result = {
            "success": False,
            "error": f"Executor error: {type(e).__name__}: {e!s}",
            "output": "",
            "plots": [],
            "execution_time": 0.0,
            "traceback": traceback.format_exc(),
        }
        json.dump(error_result, sys.stdout)
        sys.stdout.flush()
        sys.exit(1)


if __name__ == "__main__":
    main()
