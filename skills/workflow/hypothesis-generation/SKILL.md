---
name: hypothesis-generation
description: Generate testable hypotheses from data patterns and literature
category: workflow
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

Decide which hypothesis to test first by weighing how central it is to the research question, whether it can be tested with the current data, whether it would be a new insight, and how well it fits with existing findings. Test the most promising hypothesis first.

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
- H2 is the most direct explanation of the observation and fully testable with current data ⭐
- H1 is plausible and testable via product/precursor ratios
- H3 is tangential to the question

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
- You have 2-3 promising hypotheses ready to test
- You've covered the main alternative explanations
- Further brainstorming is giving diminishing returns

Don't generate 20 hypotheses - focus on quality over quantity.
