"""Documentation page."""

from nicegui import ui

from shandy.webapp_components.utils.auth import require_auth


@ui.page("/docs")
@require_auth
def docs_page():
    """Documentation page."""

    with ui.header().classes("items-center justify-between"):
        ui.label("SHANDY - Documentation").classes("text-h4")
        ui.button("Back to Jobs", on_click=lambda: ui.navigate.to("/jobs"), icon="arrow_back")

    with ui.card().classes("w-full max-w-4xl mx-auto mt-8"):
        ui.markdown("""
# SHANDY Documentation

**Scientific Hypothesis Agent for Novel Discovery**

## What is SHANDY?

SHANDY is an autonomous AI scientist that analyzes scientific data to discover mechanistic insights through iterative hypothesis testing.

## How It Works

1. **Submit a Job**: Provide a research question and optionally upload data files
2. **Autonomous Discovery**: SHANDY runs for N iterations, analyzing data and searching literature
3. **View Results**: Track progress in the Timeline view, see key findings in Summary, and download the final Report

## Features

- **Autonomous**: Runs without human intervention
- **Domain-Agnostic**: Works for metabolomics, genomics, structural biology, and more
- **Literature-Grounded**: Searches PubMed for mechanistic insights
- **Progressive Disclosure**: See high-level summaries first, drill into details on demand
- **Downloadable Visualizations**: Export plots and the final report as PDF

## Supported Data Formats

SHANDY accepts various file types:

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

## Tips for Success

1. **Clear Research Question**: Be specific about what you want to discover
2. **Clean Data**: Ensure files are properly formatted. Provide a detailed explanation of the
data file in your query if possible, including how it is formatted, any relevant details
about the file (e.g. what the column headers signify), and how the file relates to the research question.
3. **Appropriate Iterations**: 10 is sufficient for many analyses. More iterations may help with more
complicated questions.

## Support

For issues or questions, contact your system administrator.
        """)
