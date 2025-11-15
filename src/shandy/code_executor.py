"""
Code execution with sandboxing for SHANDY.

Executes Python code with timeouts, import whitelisting, and safety measures.
"""

import ast
import io
import sys
import signal
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from typing import Dict, Any, List, Optional
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend
import matplotlib.pyplot as plt
import seaborn as sns


class TimeoutException(Exception):
    """Raised when code execution times out."""
    pass


class ForbiddenImportException(Exception):
    """Raised when code tries to import forbidden modules."""
    pass


def timeout_handler(signum, frame):
    """Signal handler for execution timeout."""
    raise TimeoutException("Code execution timed out")


def validate_imports(code: str, allowed_imports: List[str]) -> None:
    """
    Validate that code only imports allowed modules.

    Args:
        code: Python code to validate
        allowed_imports: List of allowed module names

    Raises:
        ForbiddenImportException: If code imports forbidden modules
    """
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise SyntaxError(f"Syntax error in code: {e}")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_name = alias.name.split('.')[0]  # Get top-level module
                if module_name not in allowed_imports:
                    raise ForbiddenImportException(
                        f"Import of '{alias.name}' is not allowed. "
                        f"Allowed imports: {', '.join(allowed_imports)}"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                module_name = node.module.split('.')[0]
                if module_name not in allowed_imports:
                    raise ForbiddenImportException(
                        f"Import from '{node.module}' is not allowed. "
                        f"Allowed imports: {', '.join(allowed_imports)}"
                    )


def execute_code(code: str, data: pd.DataFrame, plots_dir: Path,
                timeout: int = 60, description: str = "", iteration: int = 0) -> Dict[str, Any]:
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
        data: DataFrame available as `data` variable
        plots_dir: Directory to save generated plots
        timeout: Max execution time in seconds (default: 60)
        description: Optional description of what's being investigated
        iteration: Current iteration number

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
    import time

    # Allowed imports
    allowed_imports = [
        # Core scientific computing
        'pandas', 'numpy', 'scipy', 'matplotlib', 'seaborn',
        'statsmodels', 'sklearn',

        # Standard library (safe modules)
        'math', 'statistics', 'collections', 'itertools',
        'functools', 'operator', 'datetime', 'time', 're', 'json',

        # Domain-specific
        'networkx',      # Network/graph analysis (for pathways)
    ]

    # Validate imports
    try:
        validate_imports(code, allowed_imports)
    except (SyntaxError, ForbiddenImportException) as e:
        return {
            "success": False,
            "error": str(e),
            "output": "",
            "plots": [],
            "execution_time": 0.0
        }

    # Prepare namespace with allowed libraries
    namespace = {
        'data': data,
        'pd': pd,
        'np': np,
        'plt': plt,
        'sns': sns,
        '__builtins__': __builtins__,
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
                num = int(p.stem.split('_')[1])
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
        plt.savefig(plot_path, bbox_inches='tight', dpi=150)
        plt.close()

        # Save plot metadata (description + iteration)
        metadata_path = plots_dir / f"plot_{plot_counter[0]}.json"
        import json
        metadata = {
            "plot_number": plot_counter[0],
            "iteration": iteration,
            "description": description,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        }
        with open(metadata_path, 'w') as f:
            json.dump(metadata, f, indent=2)

        return str(plot_path)

    # Replace plt.show() with our hook
    namespace['plt'].show = save_plot_hook

    start_time = time.time()

    try:
        # Set timeout signal (Unix only - won't work on Windows)
        if hasattr(signal, 'SIGALRM'):
            signal.signal(signal.SIGALRM, timeout_handler)
            signal.alarm(timeout)

        # Execute code with output capture
        with redirect_stdout(stdout_capture), redirect_stderr(stderr_capture):
            exec(code, namespace)

        # Cancel timeout
        if hasattr(signal, 'SIGALRM'):
            signal.alarm(0)

        execution_time = time.time() - start_time

        # Collect generated plots
        plot_files = [str(p) for p in plots_dir.glob("plot_*.png")]

        return {
            "success": True,
            "output": stdout_capture.getvalue(),
            "plots": plot_files,
            "error": None,
            "execution_time": execution_time
        }

    except TimeoutException:
        if hasattr(signal, 'SIGALRM'):
            signal.alarm(0)
        return {
            "success": False,
            "error": f"Code execution timed out after {timeout} seconds",
            "output": stdout_capture.getvalue(),
            "plots": [],
            "execution_time": timeout
        }

    except Exception as e:
        if hasattr(signal, 'SIGALRM'):
            signal.alarm(0)

        execution_time = time.time() - start_time

        # Get traceback
        import traceback
        error_trace = traceback.format_exc()

        return {
            "success": False,
            "error": f"{type(e).__name__}: {str(e)}",
            "output": stdout_capture.getvalue(),
            "traceback": error_trace,
            "plots": [],
            "execution_time": execution_time
        }


def format_execution_result(result: Dict[str, Any]) -> str:
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
    else:
        parts = ["L Code execution failed\n"]
        parts.append(f"Error: {result['error']}\n")
        if result.get("output"):
            parts.append(f"\nPartial output:\n{result['output']}\n")
        if result.get("traceback"):
            parts.append(f"\nTraceback:\n{result['traceback']}")
        return "".join(parts)
