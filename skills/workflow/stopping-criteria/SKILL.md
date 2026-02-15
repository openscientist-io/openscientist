---
name: stopping-criteria
description: Decide when to stop investigating and prepare final report
category: workflow
---

# Stopping Criteria

## When to Use This Skill

- When approaching iteration limit
- When considering if investigation is "done"
- When deciding between continuing vs synthesizing

## The Core Question

**"Have we learned enough to answer the research question?"**

## Hard Stops (Must Stop)

These are non-negotiable stopping points:

### 1. Iteration Budget Exhausted
- You've reached max_iterations
- **Action:** Begin final synthesis immediately

### 2. Budget Exceeded
- Cost exceeds allowed threshold
- **Action:** Stop and synthesize with what you have

### 3. Catastrophic Failure
- Data is fundamentally flawed
- Analysis approach is wrong
- **Action:** Document the issue, stop investigation

## Soft Stops (Consider Stopping)

These indicate you MIGHT be done:

### 1. Coherent Mechanistic Model

**Criteria:**
- You can explain the phenotype with a plausible mechanism
- Multiple findings support the same model
- No major contradictions remain

**Example:**
```
Research question: "Why is hypothermia neuroprotective?"

Model:
1. Hypothermia → metabolic shift to energy-efficient pathways
2. Nucleotide salvage increased (evidence: precursor depletion)
3. Membrane remodeling with PUFA enrichment (evidence: PC changes)
4. Enhanced antioxidant capacity (evidence: uronic acid pathway)

This is coherent ✓
```

**Action:** Consider stopping to synthesize

### 2. Diminishing Returns

**Signs:**
- Last 5 iterations produced no new insights
- All high-priority hypotheses tested
- Only low-impact questions remain
- Repeating similar analyses

**Example:**
```
Iteration 45: No new findings
Iteration 46: Tested marginal hypothesis (p=0.3)
Iteration 47: Re-analyzed previous finding
Iteration 48: Explored irrelevant variable
Iteration 49: ???
```

**Action:** Stop and synthesize rather than continue unproductively

### 3. Sufficient Coverage

**Criteria:**
- Tested all major pathway classes
- Addressed both positive and negative controls
- Examined key confounders (sex, age, etc.)
- Answered sub-questions of main research question

**Check:**
- [ ] Addressed the "why" not just the "what"
- [ ] Tested alternative explanations
- [ ] Identified mechanistic basis
- [ ] Connected findings into story

**Action:** If all checked, consider stopping

### 4. Satisfactory Answer Count

**Guidelines by study complexity:**
- Simple question: 2-3 major findings
- Moderate complexity: 3-5 major findings
- Complex multi-omics: 5-8 major findings

**Quality > Quantity:** Better to have 3 well-supported findings than 10 marginal ones

## Should You Continue?

### Continue if:
✅ High-priority hypotheses remain untested
✅ Recent findings opened promising new directions
✅ Current findings are contradictory (need resolution)
✅ Still < 70% of iteration budget used
✅ Each iteration is still productive

### Stop if:
🛑 Reached iteration limit
🛑 Last 5+ iterations unproductive
🛑 All high-priority hypotheses tested
🛑 Coherent mechanistic model established
🛑 No promising leads remain

## The Synthesis Test

**Try this mental exercise:**

> "Can I explain the research question to a colleague right now with supporting evidence?"

**If YES:**
- You have the core answer
- Additional iterations = diminishing returns
- **Consider stopping**

**If NO:**
- Major gaps remain
- **Continue investigating**

## Decision Framework

```
Are you at iteration limit?
│
YES → STOP (mandatory)
│
NO → Continue
    │
    Have you answered the research question with mechanistic model?
    │
    YES → STOP and synthesize
    │
    NO → Continue
        │
        Are last 5 iterations productive?
        │
        NO → STOP (diminishing returns)
        │
        YES → Continue
            │
            Do high-priority hypotheses remain?
            │
            YES → CONTINUE testing
            │
            NO → STOP and synthesize
```

## Endgame Strategy (Last 20% of Iterations)

When you're in the final 20% of your iteration budget:

### Shift Priorities

**STOP doing:**
- ❌ Broad exploration
- ❌ Low-priority hypotheses (score <30)
- ❌ Tangential questions
- ❌ Over-analyzing confirmed findings

**START doing:**
- ✅ Test remaining high-priority hypotheses only
- ✅ Record all unrecorded findings
- ✅ Resolve contradictions
- ✅ Connect findings into coherent story
- ✅ Identify knowledge gaps

### Triage Ruthlessly

**Example endgame (iterations 41-50):**
```
Iteration 41: Test last high-priority hypothesis (H8)
Iteration 42: Record findings from H8
Iteration 43: Resolve contradiction between F3 and F5
Iteration 44: Search literature for knowledge gaps
Iteration 45: Test secondary hypothesis if time
Iteration 46-50: Reserve for synthesis and cleanup
```

## Common Mistakes

❌ **Stopping too early**
- Only 1-2 findings
- Major alternative explanations untested
- Research question not actually answered

❌ **Continuing too long**
- Repeating similar analyses
- Testing trivial hypotheses
- Avoiding synthesis work

❌ **Perfectionism**
- Waiting for "complete" understanding
- Science is incremental - you won't solve everything

❌ **Ignoring negative space**
- What you DIDN'T find is also important
- Document what was ruled out

## What "Done" Looks Like

A successful investigation concludes with:

✅ **Clear answer to research question**
- "Hypothermia is neuroprotective because..."
- Backed by statistical evidence

✅ **Mechanistic model**
- Not just correlations
- Plausible biological explanation

✅ **Multiple supporting findings**
- 3-5 major discoveries
- Mutually reinforcing evidence

✅ **Acknowledged limitations**
- What couldn't be tested
- What remains unknown

✅ **Documented process**
- Knowledge graph populated
- All findings recorded
- Analysis log complete

## Final Checklist

Before stopping, verify:

- [ ] Research question is answered (or declared unanswerable)
- [ ] Major findings are recorded in knowledge graph
- [ ] Mechanistic model is articulated
- [ ] Alternative explanations were tested
- [ ] Knowledge gaps are identified
- [ ] Ready to synthesize final report

## Example: Deciding to Stop

**Situation:** Iteration 42 of 50

**Current state:**
- 5 major findings recorded
- Coherent mechanistic model
- All high-priority hypotheses tested
- Last 3 iterations only found marginal results

**Endgame options:**
1. Continue testing low-priority hypotheses (8 iterations left)
2. Stop now and synthesize

**Decision: STOP**

**Rationale:**
- Research question is answered ✓
- Mechanistic model is coherent ✓
- High-priority work is done ✓
- Continuing = diminishing returns
- Better to synthesize well than chase marginal findings

## Key Principle

**Know when to stop digging and start synthesizing.**

Perfect understanding is impossible. Good science is about answering the question with the evidence you can gather.

If you've:
1. Answered the research question
2. Built a mechanistic model
3. Tested major alternatives
4. Hit diminishing returns

**STOP and write an excellent final report.**

That's more valuable than 10 more iterations of marginal analyses.
