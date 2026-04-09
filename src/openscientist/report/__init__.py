"""Report generation with embedded plots.

Pipeline: markdown (with figure tags) → HTML → PDF (via WeasyPrint).
"""

from openscientist.report.figures import FigureCard, build_figure_inventory
from openscientist.report.processor import process_figure_tags, strip_figure_tags
from openscientist.report.renderer import render_report_html

__all__ = [
    "FigureCard",
    "build_figure_inventory",
    "process_figure_tags",
    "render_report_html",
    "strip_figure_tags",
]
