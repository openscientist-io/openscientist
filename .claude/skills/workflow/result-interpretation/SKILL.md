---
name: result-interpretation
description: Interpret statistical results and decide what to do next
---

# Result Interpretation

## When to Use This Skill

- After running a statistical test
- When deciding if a hypothesis is supported or rejected
- When planning next steps based on results

## Types of Results

### 1. Positive Finding (Hypothesis Supported)

**Criteria:**
- p-value < significance threshold (typically 0.05)
- Effect size is meaningful (not just statistically significant)
- Result makes biological sense

**What to Do:**
1. **Calculate effect size** - Don't rely on p-values alone
   - Cohen's d for t-tests
   - η² (eta-squared) for ANOVA
   - Correlation coefficient for associations

2. **Record the finding**
   ```
   update_knowledge_graph(
       title="Clear, descriptive title",
       evidence="Statistical details: p-value, effect size, confidence interval",
       interpretation="Biological meaning"
   )
   ```

3. **Search literature for validation**
   - Does this align with known biology?
   - Are there papers supporting this mechanism?
   - What enzymes/pathways are involved?

4. **Generate follow-up hypotheses**
   - What explains this finding mechanistically?
   - What are the downstream consequences?
   - What conditions would reverse this effect?

**Example:**
```
Result: CDP-Choline Synthesis Index 35.4% higher in hypothermia (p=0.042, η²=0.29)

Interpretation:
- Statistically significant (p<0.05) ✓
- Large effect size (η²=0.29 is substantial) ✓
- Suggests Pcyt1 enzyme bottleneck

Action: Record finding, search "Pcyt1 regulation hypothermia"
```

### 2. Negative Finding (Hypothesis Rejected)

**This is NOT a failure! Negative results are scientifically valuable.**

**Criteria:**
- p-value > significance threshold
- OR effect size is trivial even if p<0.05

**What to Do:**
1. **Document what was ruled out**
   - Update hypothesis status to "rejected"
   - Note the p-value and confidence interval
   - Record why this hypothesis seemed plausible

2. **Extract insights from the failure**
   - What does this non-result tell us?
   - What alternative explanations remain?
   - Did we learn about the data structure?

3. **Generate alternative hypotheses**
   - If flux wasn't increased, maybe it's a bottleneck?
   - If it's not upstream, maybe it's downstream?
   - If it's not pathway A, maybe pathway B?

**Example:**
```
Result: Salvage Flux Proxy Index - no difference (F=2.287, p=0.138)

Interpretation:
- Hypothesis rejected (p>0.05)
- But we learned: salvage flux is NOT the explanation
- This rules out 1 of 3 candidate mechanisms

Action: Generate alternative - test for enzymatic bottleneck instead
```

### 3. Borderline Result (p=0.06-0.10)

**Don't p-hack! But also don't ignore suggestive trends.**

**What to Do:**
1. **Report honestly** - "Suggestive but not significant"
2. **Check effect size** - Is it meaningful even if not "significant"?
3. **Consider:**
   - Sample size - underpowered study?
   - Measurement noise - need better proxy?
   - Confounders - should we stratify?

4. **Don't chase marginal p-values**
   - Don't add/remove outliers to get p<0.05
   - Don't try 10 different tests until one "works"
   - Move on to more promising hypotheses

### 4. Unexpected Result

**When results surprise you:**

**Example: Expected positive correlation, got negative**

1. **Check for errors first**
   - Code bugs?
   - Data quality issues?
   - Mislabeled variables?

2. **If result is real, this is interesting!**
   - Unexpected results are often the best discoveries
   - Ask: Why would this happen?
   - Search literature for mechanisms

3. **Design targeted follow-up**
   - Can we replicate in subgroups?
   - Is there a confounding variable?
   - What would explain the reversal?

## Interpreting Effect Sizes

**Cohen's d (for t-tests):**
- d = 0.2: Small effect
- d = 0.5: Medium effect
- d = 0.8: Large effect
- d > 1.2: Very large effect

**η² (for ANOVA):**
- η² = 0.01: Small effect
- η² = 0.06: Medium effect
- η² = 0.14: Large effect

**Correlation (r):**
- |r| = 0.1-0.3: Weak
- |r| = 0.3-0.5: Moderate
- |r| > 0.5: Strong

**Remember:** Large effect size with p=0.06 may be more meaningful than tiny effect with p=0.001!

## Common Interpretation Mistakes

❌ **"p>0.05 means there's no effect"**
- Wrong! It means we can't rule out chance
- Check effect size and confidence intervals

❌ **"p<0.05 means it's biologically important"**
- Wrong! Tiny effects can be "significant" with large N
- Always report effect size

❌ **"This correlation proves causation"**
- Wrong! Could be confounded, reverse causation, or coincidence
- Use mechanistic reasoning and literature

❌ **"Negative results are failures"**
- Wrong! They're scientifically valuable
- They constrain hypotheses and guide investigation

## Decision Tree: What to Do Next

```
Statistical test complete
    ├─> p<0.05 AND meaningful effect size
    │   └─> RECORD FINDING, search literature, generate follow-ups
    │
    ├─> p>0.05 (not significant)
    │   └─> HYPOTHESIS REJECTED, generate alternatives, test next hypothesis
    │
    ├─> p=0.05-0.10 (borderline)
    │   └─> NOTE AS SUGGESTIVE, check effect size, move to next hypothesis
    │
    └─> Unexpected/surprising result
        └─> VERIFY (check code), search literature, design targeted follow-up
```

## Example: Full Interpretation

**Test:** Compare nucleotide salvage precursors (Adenine, Cytidine) across groups

**Result:**
```python
# Adenine: t=-2.45, p=0.028, d=1.32
# Cytidine: t=-2.87, p=0.013, d=1.54
```

**Interpretation:**
1. Both significantly depleted (p<0.05) ✓
2. Large effect sizes (d>0.8) ✓
3. Consistent pattern (both precursors down) ✓

**Biological meaning:**
- Precursors depleted → suggests active consumption
- Could indicate increased salvage flux
- Or could indicate depleted substrate pool

**Next steps:**
1. Record finding: "Salvage precursors depleted in hypothermia"
2. Search literature: "nucleotide salvage regulation"
3. Generate hypothesis: "Is salvage flux increased?" (test with product/precursor ratio)

## Key Principle

**Every result - positive, negative, or unexpected - should generate insight and inform the next step.**

Don't just collect p-values. Think about mechanisms.
