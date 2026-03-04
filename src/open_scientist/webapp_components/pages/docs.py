"""Documentation page."""

from nicegui import ui

from open_scientist.auth import require_auth
from open_scientist.webapp_components.ui_components import render_navigator


@ui.page("/docs")
@require_auth
def docs_page() -> None:
    """Documentation page with user guide."""
    render_navigator(active_page="docs")

    with ui.column().classes("w-full max-w-4xl mx-auto mt-8 gap-4"), ui.card().classes("w-full"):
        ui.markdown(
            """
# Open Scientist Documentation

**Scientific Hypothesis Agent for Novel Discovery**

## What is Open Scientist?

Open Scientist is an autonomous AI scientist that analyzes scientific data to discover mechanistic insights through iterative hypothesis testing.

## How It Works

1. **Submit a Job**: Provide a research question and optionally upload data files
2. **Autonomous Discovery**: Open Scientist runs for N iterations, analyzing data and searching literature
3. **View Results**: Track progress in the Timeline view, see key findings in Summary, and download the final Report

## Features

- **Autonomous**: Runs without human intervention
- **Domain-Agnostic**: Works for metabolomics, genomics, structural biology, and more
- **Literature-Grounded**: Searches PubMed for mechanistic insights
- **Progressive Disclosure**: See high-level summaries first, drill into details on demand
- **Downloadable Visualizations**: Export plots and the final report as PDF

## Supported Data Formats

Open Scientist accepts various file types:

- **Tabular**: CSV, TSV, Excel (.xlsx), Parquet, JSON
- **Structures**: PDB, mmCIF (for structural biology)
- **Sequences**: FASTA
- **Images**: PNG, JPG

And many others. Data files are optional - you can also run literature-only investigations.

## Understanding Results

### Summary Tab
Shows key discoveries at a glance - the most important findings with statistical evidence.

### Timeline Tab
Chronological view of the investigation. Each iteration shows:
- What the agent investigated (plain-language summary)
- Visualizations generated (expandable)
- Literature searched (expandable with paper links)
- Findings recorded

### Report Tab
The final scientific report with:
- Executive summary
- Detailed findings with evidence
- Mechanistic interpretation
- Suggested follow-up experiments

Download as Markdown or PDF.

## API Documentation

Open Scientist provides a REST API for programmatic access. Create an API key from the
[API Keys](/api-keys) page, then use it with the `Authorization: Bearer <name>:<secret>` header.

**Interactive API documentation:**

- [Swagger UI](/api-docs) - Try API calls interactively
- [ReDoc](/api-redoc) - Detailed endpoint reference

## Tips for Success

1. **Clear Research Question**: Be specific about what you want to discover
2. **Clean Data**: Ensure files are properly formatted. Provide a detailed explanation of the
data file in your query if possible, including how it is formatted, any relevant details
about the file (e.g. what the column headers signify), and how the file relates to the research question.
3. **Appropriate Iterations**: 10 is sufficient for many analyses. More iterations may help with more
complicated questions.

## Support

For issues or questions, contact your system administrator.
            """
        )
