"""Report generation with embedded plots.

Pipeline: markdown (with figure tags) → HTML → PDF (via WeasyPrint).
"""

from openscientist.report.figures import FigureCard, build_figure_inventory
from openscientist.report.md_figure_ext import FigureExtension
from openscientist.report.processor import strip_figure_tags
from openscientist.report.renderer import render_report_html

__all__ = [
    "FigureCard",
    "FigureExtension",
    "build_figure_inventory",
    "render_report_html",
    "strip_figure_tags",
]
