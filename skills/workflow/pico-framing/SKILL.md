---
name: pico-framing
description: >
  Decompose a clinical or biomedical research question into a structured PICO
  (Population, Intervention, Comparator, Outcome) framework before the discovery
  loop begins. Outputs a PICO JSON block stored in the knowledge state. Drives
  more precise PubMed/EuropePMC queries and enables subsequent meta-analysis.
category: workflow
---

# PICO Framing

## When to Use This Skill

- At the **start** of an investigation when the question is clinical or involves a
  therapeutic/preventive intervention
- Before running a literature search for treatment efficacy, harm, or prognosis
- When the user asks to "do a systematic review" or "find evidence for X treating Y"
- Before invoking the `meta-analysis` skill (PICO framing must come first)

Do NOT use for basic science questions (e.g. "what is the mechanism of X") — PICO is
for population-level intervention evidence, not mechanistic discovery loops.

## The PICO Framework

| Letter | Stands For | Question to Answer |
|--------|------------|-------------------|
| **P** | Population | Who are the patients/participants? Age, sex, disease severity, comorbidities? |
| **I** | Intervention | What exposure/treatment/test is being evaluated? |
| **C** | Comparator | What is the control or alternative? (placebo, standard of care, different dose) |
| **O** | Outcome | What are we measuring? Primary outcome first, then secondary. |

A fifth element is sometimes added:
| **S** | Study Design | What evidence design answers this best? (RCT for efficacy; cohort for harm) |

## Workflow

### Step 1 — Parse the Research Question

Extract a raw research question from the user or from the current investigation context.

**Example input:**
> "Does azathioprine reduce relapse rates in neuromyelitis optica spectrum disorder?"

### Step 2 — Decompose into PICO

Ask yourself (or prompt the user if unclear) each PICO dimension:

```
P: Who are the patients?
   → "Adults with neuromyelitis optica spectrum disorder (NMOSD), AQP4-IgG seropositive or seronegative"

I: What is the intervention?
   → "Azathioprine (any dose, oral)"

C: What is the comparator?
   → "Placebo OR no immunosuppression OR rituximab"

O: What are the outcomes?
   → Primary: annualised relapse rate (ARR)
   → Secondary: EDSS progression, time to first relapse, adverse events
   → Timeframe: 12–24 months

S: Study design?
   → RCT preferred; cohort studies acceptable given rarity
```

### Step 3 — Output the PICO JSON Block

Write the structured PICO to the knowledge state (and to a file if in a loop):

```json
{
  "pico": {
    "population": {
      "description": "Adults with neuromyelitis optica spectrum disorder (NMOSD)",
      "include_terms": ["NMOSD", "neuromyelitis optica", "Devic disease", "AQP4-IgG"],
      "exclude_terms": ["multiple sclerosis", "children", "paediatric"],
      "mesh_terms": ["Neuromyelitis Optica[MeSH]"]
    },
    "intervention": {
      "description": "Azathioprine (oral, any dose)",
      "include_terms": ["azathioprine", "Imuran", "AZA"],
      "mesh_terms": ["Azathioprine[MeSH]"]
    },
    "comparator": {
      "description": "Placebo, no treatment, or active comparator (rituximab)",
      "include_terms": ["placebo", "no treatment", "rituximab", "mycophenolate"],
      "mesh_terms": []
    },
    "outcomes": [
      {
        "label": "primary",
        "description": "Annualised relapse rate (ARR)",
        "mesh_terms": ["Recurrence[MeSH]"]
      },
      {
        "label": "secondary",
        "description": "EDSS progression, time to first relapse, adverse events"
      }
    ],
    "study_design": ["RCT", "cohort", "case-control"],
    "timeframe": "12-24 months",
    "raw_question": "Does azathioprine reduce relapse rates in NMOSD?"
  }
}
```

### Step 4 — Build PubMed Search String

Convert the PICO JSON into a structured PubMed query:

```
(<population terms>) AND (<intervention terms>) AND (<outcome terms>) AND (<study design filter>)
```

**Example:**
```
("neuromyelitis optica"[MeSH] OR "NMOSD" OR "Devic disease")
AND ("azathioprine"[MeSH] OR "Imuran")
AND ("relapse" OR "recurrence" OR "annualised relapse rate")
AND ("randomized controlled trial"[pt] OR "cohort study"[pt] OR "clinical trial"[pt])
```

Rules for building the query:
- Use MeSH terms when available (higher recall)
- OR within each PICO dimension
- AND between dimensions
- Add NOT terms for exclusions (`NOT "children"[ti]` etc.)
- Do NOT AND on the comparator unless needed to restrict scope
- Add publication date filter if question is time-sensitive (`AND ("2010"[PDAT]:"3000"[PDAT])`)

### Step 5 — Store PICO in Knowledge State

Write to `pico.json` in the current working directory. The meta-analysis skill will read from this file.

### Step 6 — Summarise for the Agent Loop

Output a one-paragraph summary:

```
PICO framing complete:
P = Adults with NMOSD (AQP4-IgG seropositive or seronegative)
I = Azathioprine (oral, any dose)
C = Placebo, no treatment, or rituximab
O (primary) = Annualised relapse rate over 12–24 months
O (secondary) = EDSS progression, adverse events
Study design filter = RCT + cohort

PubMed query ready. Proceeding to literature search.
```

## Quality Checks

Before proceeding to literature search, verify:

- [ ] P is specific enough to retrieve relevant studies (not just "patients with disease X")
- [ ] I names the drug/intervention with synonyms and MeSH term
- [ ] C is stated even if it is "placebo" — never leave comparator empty
- [ ] O has at least one primary outcome with a measurable endpoint
- [ ] The PubMed query returns a plausible number of hits (200–2000 is ideal;
      <50 may miss studies; >5000 needs narrowing)

## PICO Framing for Rare Diseases

For rare diseases with limited RCT evidence, broaden the design filter early:
- Include case series and case reports
- Consider off-label use and natural history studies
- Accept surrogate outcomes (biomarker, imaging) when clinical outcomes are
  unavailable
- Note the evidence scarcity explicitly in the PICO summary

## Connection to Meta-Analysis Skill

After PICO framing and literature retrieval:
1. Screen abstracts against PICO inclusion criteria (P matches, I matches, comparator compatible, outcome reported)
2. Extract effect sizes per study
3. Call the `meta-analysis` domain skill with the extracted data

The `pico.json` file written here is consumed by `meta-analysis` to label forest plot axes
and check that pooled outcomes are homogeneous.
