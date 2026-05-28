---
name: Exomiser Variant Prioritization
description: >
  Run Exomiser end-to-end for phenotype-driven rare-disease variant prioritization, then
  parse, interpret, and report the results. Use whenever the job involves Exomiser, variant
  prioritisation, phenotype-driven analysis, phenopackets, VCF + HPO inputs, ranking candidate
  disease genes/variants, or generating a clinical genetics interpretation. Exomiser is
  exposed as the `run_exomiser` tool (see "Running Exomiser") — do not download, install, or
  invoke Exomiser via the shell yourself.
category: domain
tags:
  - exomiser
  - variant-prioritization
  - rare-disease
  - phenopacket
  - hpo
  - genomics
  - clinical-genetics
---

# Exomiser Variant Prioritization

This skill runs Exomiser on upstream data (VCF + phenopacket), then parses, interprets, and
reports the prioritized candidates. Exomiser is **pre-installed** in this environment — you
invoke it; you do not install it.

## Overview

[Exomiser](https://github.com/exomiser/Exomiser) prioritizes candidate disease-causing
variants from WES/WGS data using phenotype similarity (patient HPO terms vs. gene–phenotype
associations across human + model organisms), inheritance filtering, and pathogenicity scoring.

**Workflow:**
1. Confirm inputs (phenopacket with HPO terms; VCF; genome assembly).
2. Run the pre-installed Exomiser (`analyse`, default preset).
3. Parse the parquet output.
4. Emit a structured ranked candidate list (`prioritized_genes.json`).
5. Interpret the top candidates and write a clinical report.

---

## 1. Inputs

- **Phenopacket** (`.json` / `.yml`) — GA4GH phenopacket containing the patient's HPO terms
  (and often the VCF path). This is the primary input.
- **VCF** (`.vcf` / `.vcf.gz`) — the variant calls. May be referenced inside the phenopacket
  or provided separately.
- **Genome assembly** — `hg19` or `hg38` (must match the VCF).
- **PED file** (optional) — for family/trio analysis.

Extract HPO IDs from a phenopacket if you need them separately:

```python
import json
with open("patient.json") as f:
    pp = json.load(f)
hpo_ids = [
    pf["type"]["id"]
    for pf in pp.get("phenotypicFeatures", [])
    if not pf.get("excluded", False)
]
# e.g. ['HP:0001250', 'HP:0002121', ...]
```

---

## 2. Running Exomiser (via the `run_exomiser` tool)

Exomiser is exposed to you as a **tool**, `run_exomiser` — not as a shell command. **Do not run
`java`, download data, or edit configuration yourself.** Call the tool with the input files
(resolved under the job's `data/` directory):

```
run_exomiser(
    sample="patient.json",     # phenopacket (HPO terms; often includes the VCF path + assembly)
    vcf="sample.vcf.gz",       # optional if the phenopacket references it
    assembly="hg38",           # only used when vcf is supplied separately
    ped="family.ped",          # optional, for trio/family analysis
    preset="exome",            # "exome" (default) | "genome" (needs REMM) | "phenotype-only"
    description="...",         # what you're investigating
)
```

It runs Exomiser `analyse` with the recommended preset (tuned on the 100k Genomes cohort) and
returns a **JSON object**: `{ok, parquet, jsonl, output_directory, command, returncode,
stdout_tail, stderr_tail}`. **Read the `parquet` path it gives you** — don't guess filenames or
scan the folder. For full control, pass `analysis_yaml="my-analysis.yml"` (a custom Exomiser
analysis YAML in the data dir; it replaces the preset) — but use the presets unless you have a
specific reason. If you need HPO terms separately, extract them from the phenopacket (§1).

---

## 3. Parsing results

Read the **parquet** path from the `run_exomiser` result (richest data — nested phenotype
evidence, ACMG point scores, disease associations; consistent camelCase fields):

```python
import json, pandas as pd
res = json.loads(run_exomiser_result)   # the JSON the tool returned
assert res["ok"], res.get("error") or res["stderr_tail"]
df = pd.read_parquet(res["parquet"][0])
top = (df[df["isContributingVariant"] == True]
       .sort_values("geneCombinedScore", ascending=False)
       .drop_duplicates(subset=["geneSymbol", "moi"])
       .head(10))
```

Key fields:

| Field | Description |
|---|---|
| `geneCombinedScore` | Primary ranking score (0–1) — sort by this |
| `genePhenotypeScore` | Phenotype match (max of human/mouse/fish/PPI) |
| `diseasePhenotypeScore` | Human disease phenotype match |
| `geneVariantScore` | Variant pathogenicity score |
| `acmgClassification` | Automated ACMG classification |
| `acmgTotalPoints` | ACMG evidence points (comparable to ACGS 2024 thresholds) |
| `acmgProbability` | Posterior probability of pathogenicity (0–1) |
| `isContributingVariant` | `True` if the variant was used in the gene score |
| `diseaseMatches` | Patient HPO terms matched to disease terms |
| `moi` | Mode of inheritance for this gene result |

---

## 4. Structured ranked output (required) — two files

Write **two** machine-readable rankings to the job directory. Keeping them separate means the
deterministic baseline is never muddied by interpretation. Produce gene-level and/or
disease-level rankings depending on what's asked (disease-level uses the same shape with disease
IDs).

**(a) `exomiser_ranking.json` — deterministic baseline.** Exactly Exomiser's ranking: sort by
`geneCombinedScore` descending with fixed tie-breakers (`geneSymbol`, then `moi`). **Do not
re-order this list.** Use stable gene IDs (HGNC/Ensembl), not just symbols.

```json
{
  "schemaVersion": "1",
  "runMetadata": {"sampleId": "...", "assembly": "hg38",
                  "exomiserVersion": "...", "dataVersion": "..."},
  "ranking": [
    {"rank": 1, "geneSymbol": "PAH", "geneId": "HGNC:8582", "moi": "AUTOSOMAL_RECESSIVE",
     "geneCombinedScore": 0.987, "genePhenotypeScore": 0.91, "geneVariantScore": 0.99,
     "acmgClassification": "PATHOGENIC", "variants": ["12-103234567-A-G"],
     "topDiseaseMatch": "OMIM:261600 Phenylketonuria"}
  ]
}
```

**(b) `reranked.json` — formal, evidence-cited re-ranking.** This is OpenScientist's value-add:
you MAY re-order based on additional evidence (PubMed, ClinVar, OMIM, the database skills), **but
every move must be grounded in a cited source** — auditable, not a hunch. Record each item's new
rank, its `exomiserRank`, the rationale, and the evidence.

```json
{
  "schemaVersion": "1",
  "basis": "exomiser_geneCombinedScore + cited evidence",
  "ranking": [
    {"rank": 1, "geneSymbol": "...", "geneId": "...", "exomiserRank": 3,
     "rationale": "...",
     "evidence": [{"source": "ClinVar", "id": "VCV...", "note": "..."},
                  {"source": "PubMed", "id": "PMID:...", "note": "..."}]}
  ]
}
```

Rules: `exomiser_ranking.json` is the reproducible baseline — never re-order it. In
`reranked.json`, every rank change cites ≥1 source; no unsupported disease/variant claims.

---

## 5. Interpretation & report

Reason over the top candidates:
- Why each ranks highly given the patient's phenotype (cite the matched HPO/disease terms).
- Variant pathogenicity evidence (REVEL, AlphaMissense, SpliceAI, functional class, ACMG).
- Known disease associations (OMIM, Orphanet, ClinVar) — use the available database skills.
- Inheritance-pattern compatibility (`moi`).
- Strength of evidence per candidate: strong / moderate / weak / novel. Flag uncertainty.

Write a clinical report with: patient/phenotype summary, analysis summary (tool version,
assembly, date), ranked candidates table, per-candidate evidence narrative, overall
interpretation, recommended follow-up, and caveats/limitations.

---

## Reference files

Read when needed (do not load all at once):

- `references/phenopacket-parsing.md` — phenopacket v1/v2 schema and field extraction
- `references/exomiser-scores-explained.md` — full score/evidence field reference, parquet columns
- `references/clinical-interpretation-guidance.md` — ACGS 2024 reasoning, ACMG point scoring
- `references/clinical-report-template.md` — full report structure and wording
