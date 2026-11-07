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

from openscientist.artifact_packager import MAX_ARTIFACT_FILE_SIZE_BYTES
from openscientist.job_files import read_job_file_text
from openscientist.report.diagrams import DiagramExtension
from openscientist.report.icons import icon_css, uses_icons
from openscientist.report.md_figure_ext import FigureExtension
from openscientist.report.processor import isolate_figure_tags
from openscientist.webapp_components.components.badges import transform_pmid_references

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
    run_environment: str | None = None,
    excluded_top_level_dirs: tuple[str, ...] = (),
) -> str:
    """Convert markdown report to styled HTML with embedded figures.

    Args:
        markdown_path: Path to the final_report.md file.
        job_dir: Root job directory (parent of provenance/).
        embed_images: If False, use file:// paths (for WeasyPrint PDF).
            If True, use base64 data URIs (for web UI display).
        run_environment: The run's recorded environment, shown in the footer.
        excluded_top_level_dirs: Job-directory top-level entries whose files are
            not embedded, so a public reader's report never inlines uploaded inputs.

    Returns:
        Complete HTML document as a string.
    """
    # The job directory is agent-writable, so the markdown is read through the
    # symlink-safe walk; a planted link is a missing report. The size cap keeps
    # a sparse file the agent plants from reaching the web's memory.
    raw_markdown = read_job_file_text(
        job_dir,
        markdown_path.relative_to(job_dir).as_posix(),
        max_size=MAX_ARTIFACT_FILE_SIZE_BYTES,
    )
    if raw_markdown is None:
        raise FileNotFoundError(f"Markdown report not found: {markdown_path}")
    provenance_dir = job_dir / "provenance"

    # 1. Apply PMID badge transforms (before markdown conversion,
    #    since transform_pmid_references works on text/markdown)
    processed = transform_pmid_references(raw_markdown)
    # 1b. Put inline {{figure:...}} tags on their own line so the block-level
    #     FigureExtension can convert them (the model often appends them inline).
    processed = isolate_figure_tags(processed)

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
                excluded_top_level_dirs=excluded_top_level_dirs,
            ),
            DiagramExtension(),
        ],
    )

    # 4. Wrap in styled template
    css = _load_css() + (f"\n{icon_css()}" if uses_icons(body_html) else "")
    template = _load_template()
    return template.render(
        body=body_html,
        css=css,
        run_environment=run_environment,
    )
