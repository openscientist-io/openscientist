"""
Rendering and prompt-generation helpers for KnowledgeState.

These functions render the accumulated knowledge-state data (hypotheses,
findings, literature, analysis history) into the text summaries and outlines
consumed by LLM prompts. They operate on the knowledge-state ``data``
dictionary directly and perform no database access.
"""

from typing import Any

# Bound the literature abstracts dumped into the report-generation prompt.
# Dumping every abstract has overflowed the model's context window, dropping the
# tool definitions so the file-write call is never emitted. The caller passes a
# budget from the model's context window and abstracts fill it until exhausted,
# the rest omitted with a note. This default applies only when no budget is given.
_DEFAULT_REPORT_ABSTRACT_BUDGET_CHARS = 8000


def get_summary(data: dict[str, Any]) -> str:
    """
    Get a text summary of current state for prompts.

    Returns:
        Formatted summary of KS state
    """
    summary_parts = [
        f"# Knowledge Graph Summary (Iteration {data['iteration']})",
        "",
        "## Research Question",
        data["config"]["research_question"],
        "",
        "## Data",
        f"- Files: {data['data_summary'].get('files', [])}",
        f"- Samples: {data['data_summary'].get('n_samples', 'Unknown')}",
        f"- Features: {data['data_summary'].get('n_features', 'Unknown')}",
        "",
        "## Progress",
        f"- Hypotheses tested: {len([h for h in data['hypotheses'] if h['status'] != 'pending'])}",
        f"- Findings confirmed: {len(data['findings'])}",
        f"- Literature reviewed: {len(data['literature'])}",
        "",
    ]

    # Recent findings
    if data["findings"]:
        summary_parts.append("## Recent Findings")
        summary_parts.extend(
            f"- **{finding['title']}**: {finding['evidence']}" for finding in data["findings"][-3:]
        )
        summary_parts.append("")

    # Active hypotheses
    pending = [h for h in data["hypotheses"] if h["status"] == "pending"]
    if pending:
        summary_parts.append("## Pending Hypotheses")
        summary_parts.extend(f"- {hyp['id']}: {hyp['statement']}" for hyp in pending[-3:])
        summary_parts.append("")

    # Rejected hypotheses (learn from failures)
    rejected = [h for h in data["hypotheses"] if h["status"] == "rejected"]
    if rejected:
        summary_parts.append("## Rejected Hypotheses (avoid repeating)")
        summary_parts.extend(
            f"- {hyp['id']}: {hyp['statement']} - {hyp.get('result', {}).get('conclusion', 'No conclusion')}"
            for hyp in rejected[-3:]
        )
        summary_parts.append("")

    return "\n".join(summary_parts)


def get_report_summary(data: dict[str, Any]) -> str:
    """
    Get a comprehensive summary of all accumulated knowledge for report generation.

    Unlike get_summary() which is concise for iteration prompts, this includes
    ALL findings, hypotheses, literature, and iteration timelines so the report
    agent can write a thorough final report.

    Returns:
        Formatted comprehensive summary of KS state
    """
    all_hypotheses = data["hypotheses"]
    supported, rejected, pending = _split_hypotheses_by_status(all_hypotheses)

    parts = _report_intro_section(data)
    _append_data_summary_section(data, parts)
    _append_progress_overview_section(data, parts, all_hypotheses, supported, rejected, pending)
    _append_investigation_timeline_section(data, parts)
    _append_findings_section(data, parts)
    _append_supported_hypotheses_section(parts, supported)
    _append_rejected_hypotheses_section(parts, rejected)
    _append_pending_hypotheses_section(parts, pending)
    _append_literature_section(data, parts)
    _append_consensus_answer_section(data, parts)
    return "\n".join(parts)


def _split_hypotheses_by_status(
    all_hypotheses: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Split hypotheses into supported, rejected, and pending groups."""
    supported = [hyp for hyp in all_hypotheses if hyp["status"] == "supported"]
    rejected = [hyp for hyp in all_hypotheses if hyp["status"] == "rejected"]
    pending = [hyp for hyp in all_hypotheses if hyp["status"] == "pending"]
    return supported, rejected, pending


def _report_intro_section(data: dict[str, Any]) -> list[str]:
    """Build the report summary header section."""
    return [
        f"# Comprehensive Knowledge Summary (After {data['iteration']} iterations)",
        "",
        "## Research Question",
        data["config"]["research_question"],
        "",
    ]


def _append_data_summary_section(data: dict[str, Any], parts: list[str]) -> None:
    """Append data summary section when available."""
    data_summary = data.get("data_summary", {})
    if not data_summary:
        return
    parts.append("## Data Summary")
    parts.append(f"- Files: {data_summary.get('files', [])}")
    parts.append(f"- Samples: {data_summary.get('n_samples', 'Unknown')}")
    parts.append(f"- Features: {data_summary.get('n_features', 'Unknown')}")
    parts.append("")


def _append_progress_overview_section(
    data: dict[str, Any],
    parts: list[str],
    all_hypotheses: list[dict[str, Any]],
    supported: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
    pending: list[dict[str, Any]],
) -> None:
    """Append high-level counts covering hypotheses/findings/literature."""
    parts.append("## Progress Overview")
    parts.append(f"- Total hypotheses proposed: {len(all_hypotheses)}")
    parts.append(f"- Supported: {len(supported)}")
    parts.append(f"- Rejected: {len(rejected)}")
    parts.append(f"- Pending: {len(pending)}")
    parts.append(f"- Findings confirmed: {len(data['findings'])}")
    parts.append(f"- Literature reviewed: {len(data['literature'])}")
    parts.append("")


def _append_investigation_timeline_section(data: dict[str, Any], parts: list[str]) -> None:
    """Append ordered iteration summaries."""
    summaries = data.get("iteration_summaries", [])
    if not summaries:
        return
    parts.append("## Investigation Timeline")
    for entry in sorted(summaries, key=lambda item: item["iteration"]):
        strapline = entry.get("strapline", "")
        label = f" — {strapline}" if strapline else ""
        parts.append(f"- **Iteration {entry['iteration']}{label}:** {entry['summary']}")
    parts.append("")


def _append_findings_section(data: dict[str, Any], parts: list[str]) -> None:
    """Append all findings with evidence and optional metadata."""
    findings = data["findings"]
    if not findings:
        return
    parts.append("## All Findings")
    for finding in findings:
        parts.append(f"### {finding['id']}: {finding['title']}")
        parts.append(f"- **Evidence:** {finding['evidence']}")
        if finding.get("biological_interpretation"):
            parts.append(f"- **Interpretation:** {finding['biological_interpretation']}")
        if finding.get("supporting_hypotheses"):
            parts.append(
                f"- **Supporting hypotheses:** {', '.join(finding['supporting_hypotheses'])}"
            )
        if finding.get("literature_support"):
            parts.append(f"- **Literature support:** {', '.join(finding['literature_support'])}")
        if finding.get("plots"):
            parts.append(f"- **Plots:** {', '.join(finding['plots'])}")
        parts.append("")


def _append_hypothesis_result_details(parts: list[str], result: dict[str, Any]) -> None:
    """Append optional hypothesis result fields."""
    if result.get("summary"):
        parts.append(f"- **Result:** {result['summary']}")
    if result.get("p_value"):
        parts.append(f"- **P-value:** {result['p_value']}")
    if result.get("effect_size"):
        parts.append(f"- **Effect size:** {result['effect_size']}")
    if result.get("conclusion"):
        parts.append(f"- **Conclusion:** {result['conclusion']}")


def _append_supported_hypotheses_section(parts: list[str], supported: list[dict[str, Any]]) -> None:
    """Append all supported hypotheses and measured outcomes."""
    if not supported:
        return
    parts.append("## Supported Hypotheses")
    for hypothesis in supported:
        parts.append(f"### {hypothesis['id']}: {hypothesis['statement']}")
        _append_hypothesis_result_details(parts, hypothesis.get("result") or {})
        parts.append("")


def _append_rejected_hypotheses_section(parts: list[str], rejected: list[dict[str, Any]]) -> None:
    """Append rejected hypotheses with conclusions."""
    if not rejected:
        return
    parts.append("## Rejected Hypotheses")
    for hypothesis in rejected:
        parts.append(f"### {hypothesis['id']}: {hypothesis['statement']}")
        result = hypothesis.get("result") or {}
        if result.get("conclusion"):
            parts.append(f"- **Conclusion:** {result['conclusion']}")
        elif result.get("summary"):
            parts.append(f"- **Result:** {result['summary']}")
        parts.append("")


def _append_pending_hypotheses_section(parts: list[str], pending: list[dict[str, Any]]) -> None:
    """Append still-pending hypotheses as remaining knowledge gaps."""
    if not pending:
        return
    parts.append("## Knowledge Gaps (Pending Hypotheses)")
    parts.extend(f"- {hypothesis['id']}: {hypothesis['statement']}" for hypothesis in pending)
    parts.append("")


def _append_literature_section(data: dict[str, Any], parts: list[str]) -> None:
    """Append reviewed literature titles and compact abstracts."""
    literature_entries = data["literature"]
    if not literature_entries:
        return
    parts.append("## Literature Reviewed")
    for literature in literature_entries:
        pmid_str = f" (PMID: {literature['pmid']})" if literature.get("pmid") else ""
        parts.append(f"- **{literature['title']}**{pmid_str}")
        abstract = literature.get("abstract", "")
        if abstract:
            truncated = abstract[:200] + "..." if len(abstract) > 200 else abstract
            parts.append(f"  Abstract: {truncated}")
        if literature.get("relevance_to"):
            parts.append(f"  Relevant to: {', '.join(literature['relevance_to'])}")
    parts.append("")


def _append_consensus_answer_section(data: dict[str, Any], parts: list[str]) -> None:
    """Append consensus answer when one has been produced."""
    consensus_answer = data.get("consensus_answer")
    if not consensus_answer:
        return
    parts.append("## Previous Consensus Answer")
    parts.append(consensus_answer)
    parts.append("")


def get_report_outline(data: dict[str, Any], *, abstract_budget_chars: int | None = None) -> str:
    """Get an outline of accumulated knowledge for the report prompt.

    Includes finding titles, hypothesis outcomes, iteration straplines,
    and literature entries with abstracts for citation grounding.

    Args:
        data: Knowledge-state data dictionary.
        abstract_budget_chars: Total character budget for uncited-paper
            abstracts. Abstracts are included in full until the budget is
            reached, then omitted with a note. The caller derives this from
            the model's context window so the prompt cannot overflow it.
            Defaults to a conservative bound when unset.
    """
    budget = (
        abstract_budget_chars
        if abstract_budget_chars is not None
        else _DEFAULT_REPORT_ABSTRACT_BUDGET_CHARS
    )
    parts: list[str] = [
        f"# Knowledge Outline ({data['iteration']} iterations completed)",
        "",
        "## Research Question",
        data["config"]["research_question"],
        "",
    ]

    # Progress counts
    all_hyps = data["hypotheses"]
    supported = [h for h in all_hyps if h["status"] == "supported"]
    rejected = [h for h in all_hyps if h["status"] == "rejected"]
    parts.append("## Progress")
    parts.append(f"- {len(data['findings'])} findings confirmed")
    parts.append(
        f"- {len(all_hyps)} hypotheses ({len(supported)} supported, {len(rejected)} rejected)"
    )
    parts.append(f"- {len(data['literature'])} papers reviewed")
    parts.append("")

    # Investigation timeline — straplines only
    summaries = data.get("iteration_summaries", [])
    if summaries:
        parts.append("## Investigation Timeline")
        for entry in sorted(summaries, key=lambda e: e["iteration"]):
            strapline = entry.get("strapline", entry.get("summary", "")[:120])
            parts.append(f"- Iteration {entry['iteration']}: {strapline}")
        parts.append("")

    # Findings — titles with citations when available
    if data["findings"]:
        parts.append("## Findings")
        for finding in data["findings"]:
            parts.append(f"- {finding['id']}: {finding['title']}")
            if finding.get("evidence"):
                parts.append(f"  Statistical evidence: {finding['evidence']}")
            for c in finding.get("citations", []):
                status = c.get("validation_status", "unchecked")
                pmid = c.get("pmid", "?")
                snippet = c.get("snippet", "")
                explanation = c.get("explanation", "")
                parts.append(f'  - PMID:{pmid} [{status}]: "{snippet}"')
                if explanation:
                    parts.append(f"    → {explanation}")
        parts.append("")

    # Hypotheses — one-line status
    if all_hyps:
        parts.append("## Hypotheses")
        parts.extend(f"- {hyp['id']} [{hyp['status']}]: {hyp['statement']}" for hyp in all_hyps)
        parts.append("")

    # Literature — titles with abstracts for citation grounding.
    # Papers already cited by findings get title+PMID only (the snippet
    # is the grounding); uncited papers include full abstracts as fallback.
    cited_pmids: set[str] = set()
    for finding in data["findings"]:
        for c in finding.get("citations", []):
            if c.get("pmid"):
                cited_pmids.add(str(c["pmid"]))

    if data["literature"]:
        parts.append(f"## Literature ({len(data['literature'])} papers)")
        abstract_chars_spent = 0
        abstracts_omitted = 0
        for lit in data["literature"]:
            pmid_str = f" (PMID: {lit['pmid']})" if lit.get("pmid") else ""
            parts.append(f"- **{lit['title']}**{pmid_str}")
            # Abstracts are a citation-grounding fallback for papers not
            # already cited by a finding (cited papers carry their snippet).
            # Include each in full until the budget is reached, then omit the
            # rest, so a large literature list cannot overflow the context.
            if str(lit.get("pmid", "")) not in cited_pmids:
                abstract = lit.get("abstract", "")
                if not abstract:
                    continue
                if abstract_chars_spent + len(abstract) > budget:
                    abstracts_omitted += 1
                    continue
                parts.append(f"  Abstract: {abstract}")
                abstract_chars_spent += len(abstract)
        if abstracts_omitted:
            parts.append(
                f"\n_({abstracts_omitted} further abstracts omitted to fit the model "
                "context. Cite papers through the findings above, not from titles alone.)_"
            )
        parts.append("")

    # Consensus answer if exists
    if data.get("consensus_answer"):
        parts.append("## Current Consensus Answer")
        parts.append(data["consensus_answer"])
        parts.append("")

    return "\n".join(parts)
