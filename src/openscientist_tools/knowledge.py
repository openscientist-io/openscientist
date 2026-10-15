"""Standalone knowledge-state tool (`update_knowledge_state`)."""

from __future__ import annotations

from openscientist.knowledge_state import KnowledgeState
from openscientist_tools.server import mcp
from openscientist_tools.state import STATE


def update_knowledge_state(
    title: str,
    evidence: str,
    interpretation: str = "",
    description: str = "",
    citations: list[dict[str, str]] | None = None,
) -> str:
    """Record a confirmed finding to the knowledge graph.

    Args:
        title: Finding title (concise description)
        evidence: Statistical evidence (p-values, effect sizes, etc.)
        interpretation: Biological/mechanistic interpretation (optional)
        description: Why you're recording this finding
        citations: List of supporting citations from the literature. Each
            citation is a dict with keys:
            - pmid: PubMed ID of the cited paper
            - snippet: Exact quote from the paper's abstract
            - explanation: Why this quote supports the finding
            Snippets are validated against stored abstracts at recording time.

    Returns:
        Confirmation with finding number and citation validation results
    """
    ks = KnowledgeState.load_from_database_sync(STATE.job_id)
    finding_id = ks.add_finding(title=title, evidence=evidence, citations=citations)
    for f in ks.data["findings"]:
        if f["id"] == finding_id:
            f["biological_interpretation"] = interpretation
            break
    ks.log_analysis(
        action="update_knowledge_state",
        finding_id=finding_id,
        title=title,
        description=description,
    )
    ks.save_to_database_sync(STATE.job_id)
    finding_count = len(ks.data["findings"])

    parts = [f"✅ Finding #{finding_count} recorded: {title}"]
    for f in ks.data["findings"]:
        if f["id"] == finding_id:
            for c in f.get("citations", []):
                status = c.get("validation_status", "unchecked")
                pmid = c.get("pmid", "?")
                if status == "mismatch":
                    parts.append(f"⚠️  Citation PMID:{pmid} — snippet not found in abstract")
                elif status == "unchecked":
                    parts.append(f"ℹ️  Citation PMID:{pmid} — paper not in literature list")
            break
    return "\n".join(parts)


mcp.tool()(update_knowledge_state)
