"""Standalone `execute_code` tool.

Dispatches Python/Rust/SPARQL snippets into one-shot Docker
executor containers via the shared `ContainerManager`. Mirrors
the in-process tool's data-file caching, KS status updates, and
log_analysis writes.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Any

from openscientist.code_executor import format_execution_result
from openscientist.container_manager import get_container_manager
from openscientist.file_loader import get_file_info, load_data_file
from openscientist.knowledge_state import KnowledgeState
from openscientist_tools.server import mcp
from openscientist_tools.state import STATE

logger = logging.getLogger(__name__)

_DATA_CACHE: dict[str, object] = {}
_DATA_LOADED: dict[str, bool] = {}
_DATA_ERROR: dict[str, str | None] = {}


def _ensure_data_loaded() -> str | None:
    """Load STATE.data_file into the module-level cache. Returns error or None."""
    key = str(STATE.job_dir)
    if _DATA_LOADED.get(key):
        return _DATA_ERROR.get(key)

    _DATA_LOADED[key] = True

    if STATE.data_file is None:
        _DATA_ERROR[key] = None
        _DATA_CACHE[key] = None
        return None

    try:
        file_size_mb = STATE.data_file.stat().st_size / (1024 * 1024)
        print(
            f"⏳ Loading data: {STATE.data_file.name} ({file_size_mb:.1f} MB)",
            file=sys.stderr,
        )
        start = time.time()
        data = load_data_file(STATE.data_file)
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
        err = f"Unable to load data file '{STATE.data_file.name}': {e}"
        print(f"❌ {err}", file=sys.stderr)
        _DATA_ERROR[key] = err
        _DATA_CACHE[key] = None
        return err


@mcp.tool()
def execute_code(code: str, language: str = "python", description: str = "") -> str:
    """Execute code to analyze data.

    Supported languages:
    - "python" (default): Use for data analysis, statistical testing, and
      visualization. Your uploaded data files are ALREADY available in this
      tool's namespace. Do not open them by guessing filesystem paths, and do
      not reuse paths you saw in the shell (such as /agent/jobs/.../data): those
      paths do not exist in this executor. Access the data through:
        * `data`: a pandas DataFrame pre-loaded from the primary data file.
        * `data_files`: a list of dicts, one per uploaded file, each with a
          `path` key that already points to the file inside this executor
          (under /data). Read additional files with
          `pd.read_csv(data_files[i]["path"])`.
      Also available: pandas, polars, numpy, scipy, matplotlib, seaborn, plotly,
      statsmodels, pingouin, sklearn, umap-learn, leidenalg, networkx, biopython,
      scanpy, pydeseq2, and more. Plots are automatically saved to the job's
      plots directory. Choose Python unless a specific reason (performance,
      structured knowledge lookup) justifies another language.
    - "rust": Use when Python is too slow — e.g., tight inner loops over >1M rows,
      custom numerical algorithms, or performance-critical computation. Compiled and
      run with cargo. Pre-seeded crates available without imports or downloads:
      rayon (parallel iteration), ndarray + ndarray-stats (N-dimensional arrays and
      statistics), statrs (statistical distributions), rand (random numbers),
      serde + serde_json (serialization), csv (CSV parsing), anyhow (error handling),
      itertools (iterator combinators), num-traits (Float, Zero, One, etc.).
      No data or plot integration; write results to stdout.
    - "sparql": Use to query structured knowledge bases for biological, chemical, or
      scientific facts (e.g., gene functions, protein interactions, drug targets,
      taxonomic relationships). The query must include a comment specifying the
      endpoint URL, e.g.:
          # ENDPOINT: https://query.wikidata.org/sparql
      Other common endpoints: https://sparql.uniprot.org/sparql (proteins),
      https://bio2rdf.org/sparql (life sciences). Results are returned as a
      formatted table. No data or plot integration.

    Args:
        code: Code or query to execute
        language: Language to use ("python", "rust", or "sparql"). Default: "python"
        description: Optional description of what you're investigating

    Returns:
        Formatted execution result with output, plots (Python only), and any errors
    """
    if language not in ("python", "rust", "sparql"):
        return f"❌ ERROR: Unsupported language '{language}'. Supported: 'python', 'rust', 'sparql'"

    load_error = _ensure_data_loaded()
    if load_error and language not in ("rust", "sparql"):
        return f"❌ ERROR: Cannot execute code - data file failed to load.\n\n{load_error}"

    ks = KnowledgeState.load_from_database_sync(STATE.job_id)

    lang_label = {"python": "Python", "rust": "Rust", "sparql": "SPARQL"}.get(language, language)
    status_msg = f"Running {lang_label} script" if language != "sparql" else "Running SPARQL query"
    if description:
        suffix = description[:50] + "..." if len(description) > 50 else description
        status_msg = (
            f"Running {lang_label} {'query' if language == 'sparql' else 'script'}: {suffix}"
        )
    ks.set_agent_status(status_msg)
    ks.save_to_database_sync(STATE.job_id)

    provenance_dir = STATE.job_dir / "provenance"
    provenance_dir.mkdir(parents=True, exist_ok=True)

    container_mgr = get_container_manager()

    result: dict[str, Any]
    if language == "python":
        data_files: list[dict[str, Any]] = []
        for df_path in STATE.data_files:
            if not df_path.exists():
                raise FileNotFoundError(f"Data file not found: {df_path}")
            data_files.append(get_file_info(df_path))

        primary_data_path = str(STATE.data_files[0]) if STATE.data_files else None

        result = container_mgr.execute_code(
            code=code,
            job_id=STATE.job_dir.name,
            data_path=primary_data_path,
            output_dir=provenance_dir,
            timeout=60,
            description=description,
            iteration=int(ks.data["iteration"]),
            data_files=data_files,
            language="python",
        )
    else:
        result = container_mgr.execute_code(
            code=code,
            job_id=STATE.job_dir.name,
            output_dir=provenance_dir,
            timeout=300 if language == "rust" else 60,
            description=description,
            iteration=int(ks.data["iteration"]),
            language=language,
        )

    ks.log_analysis(
        action="execute_code",
        code=code,
        description=description,
        output=result.get("output", ""),
        success=result["success"],
        execution_time=result["execution_time"],
        plots=result.get("plots", []),
    )
    ks.save_to_database_sync(STATE.job_id)

    return format_execution_result(result)
