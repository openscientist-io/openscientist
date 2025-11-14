---
name: hypothesis-generation
description: Generate testable hypotheses from data patterns and literature
---

# Hypothesis Generation

## When to Use This Skill

- After identifying an interesting pattern in the data
- When a previous hypothesis was rejected (need to generate alternatives)
- At the start of investigation (bootstrap from literature)
- When you're stuck and need fresh ideas

## The Process

### 1. Review Current Knowledge

**What patterns have been observed?**
- Check the knowledge graph summary
- What group differences exist?
- What correlations are surprising?
- What contradicts expectations?

**What has been tested already?**
- Which hypotheses were supported?
- Which were rejected? (Don't repeat these!)
- What did negative results tell us?

**What does literature say?**
- Search PubMed for relevant papers
- Extract known mechanisms
- Identify knowledge gaps

### 2. Formulate Specific, Testable Hypotheses

Good hypotheses have this structure:
**"X causes Y via mechanism Z"**

**Examples:**
- ✅ Good: "Hypothermia increases nucleotide salvage flux by upregulating APRT enzyme activity"
- ❌ Too vague: "Metabolism changes in hypothermia"
- ❌ Not testable: "The brain adapts to cold"

**Requirements:**
- Must be testable with available data
- Must be falsifiable (can prove it wrong)
- Should suggest a specific analysis
- Should have mechanistic basis (not just correlation)

### 3. Prioritize Hypotheses

Score each hypothesis on:

**Impact** (1-5): How central to the research question?
- 5 = Directly explains the core phenotype
- 3 = Fills in a mechanistic detail
- 1 = Minor tangential observation

**Feasibility** (1-5): Can we test it with current data?
- 5 = Have all required variables
- 3 = Can construct proxy measure
- 1 = Missing critical data

**Novelty** (1-5): Is this a new insight?
- 5 = No one has asked this before
- 3 = Refinement of known mechanism
- 1 = Well-studied question

**Coherence** (1-5): Fits with existing findings?
- 5 = Explains contradictions or connects findings
- 3 = Extends current model
- 1 = Orthogonal to other findings

**Total Priority Score = Impact × 0.4 + Feasibility × 0.3 + Novelty × 0.2 + Coherence × 0.1**

Test highest-scoring hypotheses first.

### 4. Design the Test

For each hypothesis, specify:
- What statistical test to use
- What variables to compare
- What result would support the hypothesis
- What result would reject it

## Example Workflow

**Observation:** "CMP is elevated in hypothermia (FC=1.8, p<0.01)"

**Step 1: Search literature**
```
search_pubmed("CMP metabolism nucleotide salvage")
```
Finds: CMP is product of nucleotide salvage pathway

**Step 2: Generate hypotheses**
- H1: "Salvage flux is increased" (upstream cause)
- H2: "CMP→CDP conversion is blocked" (downstream bottleneck)
- H3: "CMP degradation is reduced" (clearance issue)

**Step 3: Prioritize**
- H1: Impact=4, Feasibility=4, Novelty=3, Coherence=4 → Score=3.8
- H2: Impact=5, Feasibility=5, Novelty=4, Coherence=4 → Score=4.6 ⭐
- H3: Impact=3, Feasibility=2, Novelty=2, Coherence=3 → Score=2.6

**Step 4: Test H2 first**
Calculate CDP-Choline Synthesis Index = CMP / CDP-Choline
Compare across groups

## Common Pitfalls to Avoid

❌ **Don't repeat rejected hypotheses**
- Check the knowledge graph for what's already been ruled out

❌ **Don't cherry-pick**
- Test hypotheses systematically, not just ones likely to succeed

❌ **Don't ignore negative results**
- Failed hypotheses are valuable - they constrain the solution space

❌ **Don't generate untestable hypotheses**
- If you can't test it with current data, it's not useful now

## When to Stop

You have enough hypotheses when:
- You have 2-3 high-priority (score >4.0) hypotheses ready to test
- You've covered the main alternative explanations
- Further brainstorming is giving diminishing returns

Don't generate 20 hypotheses - focus on quality over quantity.
