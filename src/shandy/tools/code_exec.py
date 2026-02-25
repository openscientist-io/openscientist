"""
Code execution tool for the SDK agent path.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from shandy.tools.registry import ToolContext, tool

logger = logging.getLogger(__name__)

# Lazy-loaded data state per ToolContext (process-level cache)
_DATA_CACHE: dict[str, object] = {}
_DATA_LOADED: dict[str, bool] = {}
_DATA_ERROR: dict[str, str | None] = {}


def _ensure_data_loaded(ctx: ToolContext) -> str | None:
    """Load data for ctx if not already loaded. Returns error string or None."""
    key = str(ctx.job_dir)
    if _DATA_LOADED.get(key):
        return _DATA_ERROR.get(key)

    _DATA_LOADED[key] = True

    if ctx.data_file is None:
        _DATA_ERROR[key] = None
        _DATA_CACHE[key] = None
        return None

    try:
        import sys

        from shandy.file_loader import load_data_file

        file_size_mb = ctx.data_file.stat().st_size / (1024 * 1024)
        print(
            f"⏳ Loading data: {ctx.data_file.name} ({file_size_mb:.1f} MB)",
            file=sys.stderr,
        )
        start = time.time()
        data = load_data_file(ctx.data_file)
        elapsed = time.time() - start

        if data is not None:
            print(
                f"✅ Loaded {data.shape[0]}x{data.shape[1]} in {elapsed:.1f}s",
                file=sys.stderr,
            )
        _DATA_CACHE[key] = data
        _DATA_ERROR[key] = None
        return None

    except Exception as e:
        err = f"Unable to load data file '{ctx.data_file.name}': {e}"
        print(f"❌ {err}", file=sys.stderr)
        _DATA_ERROR[key] = err
        _DATA_CACHE[key] = None
        return err


def make_tools(ctx: ToolContext) -> list[Callable[..., Any]]:
    """Return the execute_code tool bound to ctx."""

    @tool
    def execute_code(code: str, language: str = "python", description: str = "") -> str:
        """
        Execute code to analyze data.

        Supported languages:
        - "python" (default): Has access to 'data' (DataFrame), 'data_files', pandas,
          numpy, matplotlib, seaborn, scipy, sklearn, and more. Plots are automatically
          saved to the job's plots directory.
        - "rust": Compiles and runs Rust code with rustc. Useful for performance-critical
          computation. No data or plot integration; use stdout for output.
        - "sparql": Executes a SPARQL SELECT query against a remote endpoint. The query
          must include a comment specifying the endpoint URL, e.g.:
              # ENDPOINT: https://query.wikidata.org/sparql
          Results are returned as a formatted table. No data or plot integration.

        Args:
            code: Code or query to execute
            language: Language to use ("python", "rust", or "sparql"). Default: "python"
            description: Optional description of what you're investigating

        Returns:
            Formatted execution result with output, plots (Python only), and any errors
        """
        from shandy.code_executor import execute_code as exec_code
        from shandy.code_executor import (
            execute_rust_code,
            execute_sparql_code,
            format_execution_result,
        )
        from shandy.file_loader import get_file_info
        from shandy.knowledge_state import KnowledgeState
        from shandy.settings import get_settings

        if language not in ("python", "rust", "sparql"):
            return f"❌ ERROR: Unsupported language '{language}'. Supported: 'python', 'rust', 'sparql'"

        load_error = _ensure_data_loaded(ctx)
        if load_error and language not in ("rust", "sparql"):
            return f"❌ ERROR: Cannot execute code - data file failed to load.\n\n{load_error}"

        ks = KnowledgeState.load(ctx.job_dir / "knowledge_state.json")

        # Auto-set status so the UI shows what's running without the model needing to call set_status
        lang_label = {"python": "Python", "rust": "Rust", "sparql": "SPARQL"}.get(
            language, language
        )
        status_msg = (
            f"Running {lang_label} script" if language != "sparql" else "Running SPARQL query"
        )
        if description:
            suffix = description[:50] + "..." if len(description) > 50 else description
            status_msg = (
                f"Running {lang_label} {'query' if language == 'sparql' else 'script'}: {suffix}"
            )
        ks.set_agent_status(status_msg)
        ks.save(ctx.job_dir / "knowledge_state.json")

        provenance_dir = ctx.job_dir / "provenance"
        provenance_dir.mkdir(parents=True, exist_ok=True)

        if language in ("rust", "sparql"):
            if get_settings().container.use_container_isolation:
                from shandy.container_manager import get_container_manager

                container_mgr = get_container_manager()
                result = container_mgr.execute_code(
                    code=code,
                    job_id=ctx.job_dir.name,
                    output_dir=provenance_dir,
                    timeout=60,
                    description=description,
                    iteration=int(ks.data["iteration"]),
                    language=language,
                )
            elif language == "rust":
                result = execute_rust_code(
                    code,
                    provenance_dir,
                    timeout=60,
                    description=description,
                    iteration=int(ks.data["iteration"]),
                )
            else:
                result = execute_sparql_code(
                    code,
                    provenance_dir,
                    timeout=60,
                    description=description,
                    iteration=int(ks.data["iteration"]),
                )
        else:
            data = _DATA_CACHE.get(str(ctx.job_dir))

            # Build data_files metadata list from all data files in context
            data_files = []
            for df_path in ctx.data_files:
                if not df_path.exists():
                    raise FileNotFoundError(f"Data file not found: {df_path}")
                data_files.append(get_file_info(df_path))

            primary_data_path = str(ctx.data_files[0]) if ctx.data_files else None

            if get_settings().container.use_container_isolation:
                from shandy.container_manager import get_container_manager

                container_mgr = get_container_manager()
                result = container_mgr.execute_code(
                    code=code,
                    job_id=ctx.job_dir.name,
                    data_path=primary_data_path,
                    output_dir=provenance_dir,
                    timeout=60,
                    description=description,
                    iteration=int(ks.data["iteration"]),
                    data_files=data_files,
                    language="python",
                )
            else:
                import pandas as pd

                df_data: pd.DataFrame | None = data if isinstance(data, pd.DataFrame) else None
                result = exec_code(
                    code,
                    df_data,
                    provenance_dir,
                    timeout=60,
                    description=description,
                    iteration=int(ks.data["iteration"]),
                    data_files=data_files,
                )

        ks.log_analysis(
            action="execute_code",
            description=description,
            output=result.get("output", ""),
            success=result["success"],
            execution_time=result["execution_time"],
            plots=result.get("plots", []),
        )
        ks.save(ctx.job_dir / "knowledge_state.json")

        return format_execution_result(result)

    return [execute_code]
