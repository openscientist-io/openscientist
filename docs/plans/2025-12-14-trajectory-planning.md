# Trajectory Planning Proposal

**Date:** 2025-12-14
**Status:** Draft / Under Discussion

## Problem Statement

Agents currently don't plan how to use their iteration budget. Observed issues:

1. **Completing everything in iteration 1**: Agent treats the task as "do everything now" rather than "plan how to use my budget" (see job_c0ba7bf7)
2. **No early stopping**: No mechanism for agent to signal "I'm done" before max iterations
3. **No pacing**: Agent doesn't differentiate between 3-iteration and 50-iteration budgets

## Proposed Solution

### Core Idea

1. **Iteration 1 is planning-only**: Agent understands data, searches literature, creates trajectory plan
2. **Plan is saved to knowledge_state**: Persisted and shown in subsequent iteration prompts
3. **Subsequent iterations execute the plan**: Agent follows the plan, adapting as needed
4. **Early completion**: Agent uses `complete_investigation` when done

### Trajectory Plan Structure

```json
{
  "trajectory_plan": {
    "created_at_iteration": 1,
    "estimated_iterations_needed": 5,
    "complexity_assessment": "simple|moderate|complex",
    "phases": [
      {
        "name": "exploration",
        "goals": ["Understand data structure", "Initial literature review"]
      },
      {
        "name": "hypothesis_testing",
        "goals": ["Test H1: X correlates with Y", "Test H2: Z pathway involved"]
      },
      {
        "name": "synthesis",
        "goals": ["Connect findings", "Build mechanistic model"]
      }
    ],
    "current_phase": "exploration",
    "adaptations": []
  }
}
```

### Key Design Decisions

1. **No iteration numbers in phases**: Phases are named with goals, not mapped to specific iterations. This allows flexibility as the number of iterations varies.

2. **Agent estimates iterations needed**: The agent assesses complexity and estimates how many iterations it needs. For simple tasks (analyze this image), might be 2. For complex multi-omics, might be 10+.

3. **Plan is strong guidance, not rigid**: Agent should follow the plan but can adapt as findings emerge. Adaptations are noted in the plan.

4. **Updating the plan**: Rather than a separate `update_trajectory_plan` tool, the agent notes phase transitions and adaptations in `save_iteration_summary`. If plan changes significantly, agent explains in the summary.

### Example: Simple Task (2 iterations)

Research question: "Analyze this image and describe what you see"

```json
{
  "trajectory_plan": {
    "created_at_iteration": 1,
    "estimated_iterations_needed": 2,
    "complexity_assessment": "simple",
    "phases": [
      {"name": "analysis", "goals": ["Examine image", "Identify key features", "Report findings"]}
    ],
    "current_phase": "analysis"
  }
}
```

Agent completes in iteration 2 and calls `complete_investigation`.

### Example: Complex Task (8 iterations)

Research question: "Identify metabolic pathways altered in hypothermia"

```json
{
  "trajectory_plan": {
    "created_at_iteration": 1,
    "estimated_iterations_needed": 8,
    "complexity_assessment": "complex",
    "phases": [
      {"name": "exploration", "goals": ["Data QC", "Initial distributions", "Literature context"]},
      {"name": "hypothesis_testing", "goals": ["Test energy metabolism changes", "Test oxidative stress markers", "Test membrane composition"]},
      {"name": "deep_dive", "goals": ["Follow up on significant findings", "Resolve contradictions"]},
      {"name": "synthesis", "goals": ["Build mechanistic model", "Final report"]}
    ],
    "current_phase": "exploration"
  }
}
```

## Implementation Plan

### Phase 1: Add `save_trajectory_plan` tool

New MCP tool for iteration 1:
```python
@mcp.tool()
def save_trajectory_plan(
    estimated_iterations: int,
    complexity: str,  # simple, moderate, complex
    phases: List[Dict[str, Any]]
) -> str:
    """Save the investigation trajectory plan (call in iteration 1)."""
```

### Phase 2: Modify iteration 1 prompt

Make iteration 1 prompt require planning:
- "This is iteration 1. Your task is to create a trajectory plan."
- "Explore the data to understand its structure"
- "Search literature to understand the domain"
- "Create a trajectory plan with estimated iterations and phases"
- "Do NOT execute the full investigation yet"

### Phase 3: Show plan in subsequent prompts

In iterations 2+, show the plan:
- "Your trajectory plan (created in iteration 1):"
- Show phases and current phase
- "You are currently in the [X] phase. Goals: ..."

### Phase 4: Update orchestrator

- Check for `investigation_complete` flag after each iteration
- If set, stop the loop early
- Log early completion reason

## Open Questions

1. **What if agent ignores planning and does everything in iteration 1 anyway?**
   - Could enforce by limiting what tools are available in iteration 1?
   - Or just rely on strong prompt guidance?

2. **Should we enforce minimum iterations?**
   - Even for "simple" tasks, require at least 2 iterations (plan + execute)?
   - Or let agent decide?

3. **How to handle plan failures?**
   - What if the plan was wrong (data doesn't support hypotheses)?
   - Agent should adapt, but how explicit should this be?

4. **Should phases have expected iteration counts?**
   - e.g., "exploration: 1-2 iterations, hypothesis_testing: 3-5 iterations"
   - Or keep it loose and let agent manage?

## Current State

- [x] `complete_investigation` tool added (commit 2b0cdf8)
- [ ] `save_trajectory_plan` tool
- [ ] Iteration 1 prompt modification
- [ ] Show plan in subsequent prompts
- [ ] Orchestrator early completion handling
