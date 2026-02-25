"""
Tool registry for the SDK agent path.

ToolContext carries per-job state (job_dir, data_file) that is captured
in closures by each make_tools() factory.  This avoids globals and lets
multiple jobs run in the same process safely.

The @tool decorator bridges old-style tool functions (plain typed functions
returning str) to the new claude_agent_sdk tool() API which requires
(name, description, input_schema) and an async handler returning dict.

The claude_agent_sdk package is a required dependency.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import UnionType
from typing import Any, Union, get_args, get_origin, get_type_hints

from claude_agent_sdk import tool as sdk_tool

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Type-annotation → JSON Schema helpers
# ---------------------------------------------------------------------------


def _python_type_to_json_schema(tp: Any) -> dict[str, Any]:
    """Convert a Python type annotation to a JSON Schema property dict."""
    origin = get_origin(tp)
    args = get_args(tp)

    # Optional[X] is Union[X, None] or X | None — unwrap to X
    if origin in (Union, UnionType):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _python_type_to_json_schema(non_none[0])

    # list[X] → {"type": "array", "items": ...}
    if origin is list:
        schema: dict[str, Any] = {"type": "array"}
        if args:
            schema["items"] = _python_type_to_json_schema(args[0])
        return schema

    # dict[K, V] → {"type": "object"}
    if origin is dict:
        return {"type": "object"}

    type_map: dict[type, dict[str, str]] = {
        str: {"type": "string"},
        int: {"type": "integer"},
        float: {"type": "number"},
        bool: {"type": "boolean"},
        list: {"type": "array"},
        dict: {"type": "object"},
    }
    return dict(type_map.get(tp, {"type": "string"}))


def _build_input_schema(fn: Callable[..., Any]) -> dict[str, Any]:
    """Build a JSON Schema dict from a function's type hints and signature."""
    try:
        hints = get_type_hints(fn)
    except Exception:
        logger.debug("Could not resolve type hints for %s", fn.__name__, exc_info=True)
        hints = {}
    hints.pop("return", None)

    sig = inspect.signature(fn)
    properties: dict[str, Any] = {}
    required: list[str] = []

    for param_name, param in sig.parameters.items():
        tp = hints.get(param_name, str)
        properties[param_name] = _python_type_to_json_schema(tp)
        if param.default is inspect.Parameter.empty:
            required.append(param_name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = required
    return schema


def _extract_description(fn: Callable[..., Any]) -> str:
    """Extract the first paragraph of a function's docstring."""
    doc = inspect.getdoc(fn) or ""
    if not doc:
        return fn.__name__
    # First paragraph = everything before the first blank line
    return doc.split("\n\n")[0].strip()


# ---------------------------------------------------------------------------
# SDK bridge decorator
# ---------------------------------------------------------------------------


def tool(fn: Callable[..., Any]) -> Any:
    """Bridge old-style @tool functions to the new SDK tool() API.

    Extracts name, description, and input_schema from the original
    function's ``__name__``, docstring, and type hints, then creates
    an async handler that calls the original function and wraps its
    string return value in the SDK-expected dict format.
    """
    name = fn.__name__
    description = _extract_description(fn)
    input_schema = _build_input_schema(fn)

    sig = inspect.signature(fn)
    defaults = {
        p.name: p.default
        for p in sig.parameters.values()
        if p.default is not inspect.Parameter.empty
    }
    is_async = asyncio.iscoroutinefunction(fn)

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        # Merge defaults for parameters the caller didn't supply
        full_args = {**defaults, **args}
        if is_async:
            result = await fn(**full_args)
        else:
            result = await asyncio.to_thread(fn, **full_args)
        return {"content": [{"type": "text", "text": str(result)}]}

    return sdk_tool(name, description, input_schema)(handler)


# ---------------------------------------------------------------------------
# ToolContext and build_tool_list
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolContext:
    """Per-job context captured in tool closures."""

    job_dir: Path
    data_file: Path | None = None
    data_files: tuple[Path, ...] = ()


def build_tool_list(
    job_dir: Path,
    data_file: Path | None = None,
    use_hypotheses: bool = False,
    data_files: list[Path] | None = None,
) -> list[Any]:
    """
    Build the full list of SDK tool objects for a job.

    Each item is an ``SdkMcpTool`` instance created by the SDK bridge decorator.

    Args:
        job_dir: Path to the job directory
        data_file: Optional path to the primary data file
        use_hypotheses: Whether to include hypothesis tracking tools
        data_files: All data files for this job. Falls back to [data_file] when
            not provided so callers that only set data_file keep working.
    """
    resolved_files: tuple[Path, ...]
    if data_files is not None:
        resolved_files = tuple(data_files)
    elif data_file is not None:
        resolved_files = (data_file,)
    else:
        resolved_files = ()
    ctx = ToolContext(job_dir=job_dir, data_file=data_file, data_files=resolved_files)

    from shandy.tools.code_exec import make_tools as code_tools
    from shandy.tools.document import make_tools as doc_tools
    from shandy.tools.job_meta import make_tools as meta_tools
    from shandy.tools.knowledge import make_tools as knowledge_tools
    from shandy.tools.pubmed import make_tools as pubmed_tools

    tools: list[Any] = []
    tools.extend(pubmed_tools(ctx))
    tools.extend(knowledge_tools(ctx, use_hypotheses=use_hypotheses))
    tools.extend(code_tools(ctx))
    tools.extend(doc_tools(ctx))
    tools.extend(meta_tools(ctx))

    # Phenix tools are optional (only if Phenix is configured)
    try:
        from shandy.tools.phenix import make_tools as phenix_tools

        tools.extend(phenix_tools(ctx))
    except ImportError:
        pass

    return tools
