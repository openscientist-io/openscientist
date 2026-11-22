# HCMO evidence export prototype

This synthetic example exports one normalized OpenScientist job as a
machine-verifiable HCMO + PROV-O + STATO evidence bundle. It is an offline,
opt-in prototype and does not change the production job lifecycle.

Run from the repository root:

```bash
uv run python -m openscientist.evidence.hcmo_export \
  --snapshot examples/hcmo_evidence/job-snapshot.json \
  --source-root examples/hcmo_evidence \
  --report examples/hcmo_evidence/final_report.md \
  --output-dir examples/hcmo_evidence/results
```

The CLI confines all caller-selected paths to the current directory by
default. Use `--workspace-root PATH` when running from another directory; the
snapshot, source root, report, custom vocabulary files, and output directory
must remain within that root.

The command writes `semantic-manifest.json`, `evidence.ttl`,
`validation.json`, `traceability-appendix.md`, and
`final_report_with_traceability.md`. It exits zero only when RDF syntax,
SHACL, the closed vocabulary, citation grounding, semantic-pin structure, and
source-file hashes all pass.

The snapshot preserves current `KnowledgeState.to_dict()` keys and adds the
explicit prototype fields `data_files`, `statistical_results`, and
`semantic_context`. It also includes a `semantic_manifest` that freezes the
evidence contract and external-vocabulary version IRIs/hashes. Findings carry
`analysis_ids`, `data_file_ids`, `result_ids`, `inference_scope`, and
`experimental_unit_count`. OpenScientist does not persist all of these fields
today; requiring them prevents the exporter from inventing provenance or
hiding the scope of a claim.

See [the design note](../../docs/HCMO_EVIDENCE_EXPORT.md) for scope,
limitations, and a possible production integration path.
