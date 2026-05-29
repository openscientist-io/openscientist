"""Reference validation and bibliography generation for agent-authored reports.

Catches the failure mode where an agent writes ``Surname et al. YEAR (PMID)``
but the cited Surname is not on the actual paper's author list (real example:
"Lopera et al. 2025 (PMID: 40637118)" — Lopera doesn't appear among the 15
authors). Also generates a proper deduplicated References section.

See :mod:`openscientist.references.validator` for the entry points.
"""
