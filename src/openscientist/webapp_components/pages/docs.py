"""Documentation page."""

from textwrap import dedent

from nicegui import ui

from openscientist.auth import require_auth
from openscientist.webapp_components.ui_components import (
    render_navigator,
    render_project_resource_links,
)

DOCS_PAGE_CONTENT_MARKDOWN = dedent(
    """
    ## What is OpenScientist?

    OpenScientist is an autonomous AI scientist that analyzes scientific data to discover mechanistic
    insights through iterative hypothesis testing.

    ## How It Works

    1. **Submit a Job**: Provide a research question and optionally upload data files.
    2. **Autonomous Discovery**: OpenScientist runs for N iterations, analyzing data and searching literature.
    3. **View Results**: Track progress in the Timeline view, see key findings in Summary, and download the final report.

    ## Features

    - **Autonomous**: Runs without human intervention.
    - **Domain-Agnostic**: Works for metabolomics, genomics, structural biology, and more.
    - **Literature-Grounded**: Searches PubMed for mechanistic insights.
    - **Progressive Disclosure**: See high-level summaries first, drill into details on demand.
    - **Downloadable Visualizations**: Export plots and the final report as PDF.

    ## Supported Data Formats

    OpenScientist accepts various file types:

    - **Tabular**: CSV, TSV, Excel (.xlsx), Parquet, JSON.
    - **Structures**: PDB, mmCIF (for structural biology).
    - **Sequences**: FASTA.
    - **Images**: PNG, JPG.

    And many others. Data files are optional; you can also run literature-only investigations.

    ## Understanding Results

    ### Summary Tab

    Shows key discoveries at a glance: the most important findings with statistical evidence.

    ### Timeline Tab

    Chronological view of the investigation. Each iteration shows:

    - What the agent investigated (plain-language summary).
    - Visualizations generated (expandable).
    - Literature searched (expandable with paper links).
    - Findings recorded.

    ### Report Tab

    The final scientific report includes:

    - Summary.
    - Detailed findings with evidence.
    - Mechanistic interpretation.
    - Suggested follow-up experiments.

    Download the report as Markdown or PDF.

    ## API Documentation

    OpenScientist provides a REST API for programmatic access. Create an API key from the
    [API Keys](/api-keys) page, then use it with the `Authorization: Bearer <name>:<secret>` header.

    **Interactive API documentation:**

    - [Swagger UI](/api-docs): Try API calls interactively.
    - [ReDoc](/api-redoc): Detailed endpoint reference.

    ## Tips for Success

    1. **Clear Research Question**: Be specific about what you want to discover.
    2. **Clean Data**: Ensure files are properly formatted. Provide a detailed explanation of the
       data file in your query when possible, including how it is formatted, what the column headers
       mean, and how the file relates to the research question.
    3. **Appropriate Iterations**: Ten iterations are sufficient for many analyses. More iterations may
       help with more complicated questions.

    ## Support

    For issues or questions, contact your system administrator.
    """
).strip()


@ui.page("/docs")
@require_auth
def docs_page() -> None:
    """Documentation page with user guide."""
    render_navigator(active_page="docs")

    with ui.column().classes("w-full max-w-4xl mx-auto mt-8 gap-4"), ui.card().classes("w-full"):
        ui.markdown(
            """
# OpenScientist Documentation

**Scientific Hypothesis Agent for Novel Discovery**
            """
        )
        render_project_resource_links(include_docs=False)
        ui.separator().classes("w-full my-2")
        ui.markdown(DOCS_PAGE_CONTENT_MARKDOWN)
