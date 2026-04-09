"""Figure inventory builder.

Scans provenance/ for plot PNGs and their JSON metadata to build a catalog
of available figures for the report agent.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FigureCard:
    """Metadata for a single plot available for the report."""

    figure_id: str
    filename: str
    path: Path
    iteration: int | None = None
    description: str = ""
    finding_ids: list[str] = field(default_factory=list)


def build_figure_inventory(job_dir: Path) -> list[FigureCard]:
    """Scan provenance/*.json + orphaned *.png for plot metadata.

    Primary: parse companion JSON metadata files.
    Fallback: create synthetic entries for PNGs without JSON metadata.

    Args:
        job_dir: Root job directory containing provenance/.

    Returns:
        List of FigureCard entries sorted by iteration then filename.
    """
    provenance_dir = job_dir / "provenance"
    if not provenance_dir.exists():
        return []

    cards: list[FigureCard] = []
    seen_pngs: set[str] = set()

    # Primary: scan JSON metadata files for plot info
    for json_file in sorted(provenance_dir.glob("*.json")):
        png_file = json_file.with_suffix(".png")
        if not png_file.exists():
            continue

        try:
            metadata = json.loads(json_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            logger.debug("Skipping unreadable metadata: %s", json_file)
            continue

        # Skip non-plot JSON files (e.g. iteration transcripts)
        if "iteration" not in metadata and "description" not in metadata:
            continue

        seen_pngs.add(png_file.name)
        cards.append(
            FigureCard(
                figure_id=png_file.stem,
                filename=png_file.name,
                path=png_file,
                iteration=metadata.get("iteration"),
                description=metadata.get("description", ""),
                finding_ids=metadata.get("finding_ids", []),
            )
        )

    # Fallback: orphaned PNGs without JSON metadata
    for png_file in sorted(provenance_dir.glob("*.png")):
        if png_file.name in seen_pngs:
            continue
        cards.append(
            FigureCard(
                figure_id=png_file.stem,
                filename=png_file.name,
                path=png_file,
                description=png_file.stem.replace("_", " ").title(),
            )
        )

    # Sort by iteration (None last) then filename
    cards.sort(key=lambda c: (c.iteration if c.iteration is not None else 9999, c.filename))
    return cards


def format_figure_inventory_prompt(cards: list[FigureCard]) -> str:
    """Format figure inventory as a prompt section for the report agent.

    Args:
        cards: List of FigureCard entries.

    Returns:
        Markdown-formatted section listing available figures.
    """
    if not cards:
        return ""

    lines = [
        "## Available Figures",
        "",
        "The following plots were generated during the investigation. "
        "You may embed them in the report using the syntax "
        "`{{figure:filename.png|caption=Your caption here}}`.",
        "",
        "Select the most informative plots — aim for ~1 figure per major finding.",
        "",
    ]
    for card in cards:
        iteration_str = f" (iteration {card.iteration})" if card.iteration else ""
        finding_str = f" [findings: {', '.join(card.finding_ids)}]" if card.finding_ids else ""
        lines.append(f"- `{card.filename}`{iteration_str}: {card.description}{finding_str}")

    lines.append("")
    return "\n".join(lines)
