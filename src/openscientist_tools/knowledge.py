"""Standalone knowledge-state tools (`update_knowledge_state`,
`add_hypothesis`, `update_hypothesis`).

`add_hypothesis` and `update_hypothesis` are registered only when
`STATE.use_hypotheses` is true (bound from the
`OPENSCIENTIST_USE_HYPOTHESES` env var). All three function bodies
are defined unconditionally so tests can import and call them
in-process regardless of the env state.
"""

from __future__ import annotations

from openscientist.knowledge_state import KnowledgeState
from openscientist_tools.server import mcp
from openscientist_tools.state import STATE

_VALID_STATUSES = ["pending", "testing", "supported", "rejected"]
_STATUS_EMOJI = {"testing": "🔬", "supported": "✅", "rejected": "❌"}


async def update_knowledge_state(
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
    ks = await KnowledgeState.load_from_database(STATE.job_id)
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
    await ks.save_to_database(STATE.job_id)
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


async def add_hypothesis(statement: str) -> str:
    """Add a new hypothesis to test.

    Args:
        statement: The hypothesis statement (e.g., 'X increases Y under Z conditions')

    Returns:
        Confirmation with hypothesis ID
    """
    ks = await KnowledgeState.load_from_database(STATE.job_id)
    hyp_id = ks.add_hypothesis(statement=statement, proposed_by="agent")
    ks.log_analysis(action="add_hypothesis", hypothesis_id=hyp_id, statement=statement)
    await ks.save_to_database(STATE.job_id)
    return f"✅ Hypothesis {hyp_id} added: {statement}"


async def update_hypothesis(
    hypothesis_id: str,
    status: str,
    result_summary: str = "",
    p_value: str = "",
    effect_size: str = "",
    conclusion: str = "",
) -> str:
    """Update a hypothesis with test results.

    Args:
        hypothesis_id: Hypothesis ID (e.g., 'H001')
        status: New status - must be one of:
                - "testing" - currently being tested
                - "supported" - evidence supports the hypothesis
                - "rejected" - evidence contradicts the hypothesis
        result_summary: Brief summary of test results
        p_value: P-value from statistical test (as string, e.g., "p=0.003")
        effect_size: Effect size (e.g., "Cohen's d=0.8", "r=0.45")
        conclusion: What this means for the research question

    Returns:
        Confirmation of update
    """
    if status not in _VALID_STATUSES:
        return f"❌ Invalid status '{status}'. Must be one of: {', '.join(_VALID_STATUSES)}"

    ks = await KnowledgeState.load_from_database(STATE.job_id)

    updates: dict[str, object] = {"status": status}
    if status in ("supported", "rejected"):
        updates["tested_at_iteration"] = ks.data["iteration"]
        updates["result"] = {
            "summary": result_summary,
            "p_value": p_value,
            "effect_size": effect_size,
            "conclusion": conclusion,
        }

    try:
        ks.update_hypothesis(hypothesis_id=hypothesis_id, updates=updates)
    except ValueError as e:
        return f"❌ {e}"

    ks.log_analysis(
        action="update_hypothesis",
        hypothesis_id=hypothesis_id,
        status=status,
        result_summary=result_summary,
    )
    await ks.save_to_database(STATE.job_id)

    emoji = _STATUS_EMOJI.get(status, "📝")
    return f"{emoji} Hypothesis {hypothesis_id} updated to '{status}'"


mcp.tool()(update_knowledge_state)

if STATE.use_hypotheses:
    mcp.tool()(add_hypothesis)
    mcp.tool()(update_hypothesis)
