"""HTML report renderer.

Converts markdown report (with figure tags) to styled HTML via:
1. Convert markdown → HTML via the ``markdown`` library (with FigureExtension)
2. Apply PMID badge transforms
3. Wrap in Jinja2 template with professional CSS
"""

from __future__ import annotations

import logging
from pathlib import Path

import jinja2
import markdown as md  # type: ignore[import-untyped]

from openscientist.report.md_figure_ext import FigureExtension
from openscientist.webapp_components.ui_components import transform_pmid_references

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).parent / "templates"


def _load_css() -> str:
    """Load the report CSS from the templates directory."""
    css_path = _TEMPLATE_DIR / "report.css"
    return css_path.read_text(encoding="utf-8")


def _load_template() -> jinja2.Template:
    """Load the Jinja2 HTML report template."""
    loader = jinja2.FileSystemLoader(str(_TEMPLATE_DIR))
    env = jinja2.Environment(loader=loader, autoescape=False)
    return env.get_template("report.html.j2")


def render_report_html(
    markdown_path: Path,
    job_dir: Path,
    *,
    embed_images: bool = False,
) -> str:
    """Convert markdown report to styled HTML with embedded figures.

    Args:
        markdown_path: Path to the final_report.md file.
        job_dir: Root job directory (parent of provenance/).
        embed_images: If False, use file:// paths (for WeasyPrint PDF).
            If True, use base64 data URIs (for web UI display).

    Returns:
        Complete HTML document as a string.
    """
    raw_markdown = markdown_path.read_text(encoding="utf-8")
    provenance_dir = job_dir / "provenance"

    # 1. Apply PMID badge transforms (before markdown conversion,
    #    since transform_pmid_references works on text/markdown)
    processed = transform_pmid_references(raw_markdown)

    # 2. Convert markdown → HTML
    #    FigureExtension handles {{figure:...}} tags and image path resolution
    body_html = md.markdown(
        processed,
        extensions=[
            "tables",
            "fenced_code",
            "toc",
            "attr_list",
            FigureExtension(
                provenance_dir=str(provenance_dir),
                use_base64=embed_images,
            ),
        ],
    )

    # 4. Wrap in styled template
    css = _load_css()
    template = _load_template()
    return template.render(
        body=body_html,
        css=css,
    )
