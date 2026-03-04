"""
SHANDY Executor main entry point.

Reads execution requests from stdin JSON and writes execution results to stdout JSON.
"""

import json
import sys
import traceback
from pathlib import Path

from shandy.code_executor import (
    execute_code,
    execute_rust_code,
    execute_sparql_code,
    load_data,
)


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
                plots_dir=output_dir,
                timeout=timeout,
                description=description,
                iteration=iteration,
                data_files=data_files,
            )
        elif language == "sparql":
            result = execute_sparql_code(
                code=code,
                plots_dir=output_dir,
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
                plots_dir=output_dir,
                timeout=timeout,
                description=description,
                iteration=iteration,
                data_files=data_files,
                save_code_with_plots=True,
            )

        # Write result to stdout
        json.dump(result, sys.stdout)
        sys.stdout.flush()

    except Exception as e:
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
