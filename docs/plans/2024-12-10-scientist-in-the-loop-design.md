# Scientist-in-the-Loop Feature Design

**Date:** 2024-12-10
**Status:** Design Complete

## Problem Statement

SHANDY currently runs autonomously without scientist input during execution. Scientists may want to:
- Redirect the agent when it's going down a wrong path
- Inject domain expertise the agent might miss
- Reprioritize investigations based on early results

## Design Decision: Two Job Modes

After brainstorming, we decided on a **two-mode approach** that gives scientists flexibility:

### Mode 1: Autonomous (Default)
- Job runs to completion without pausing
- No feedback mechanism during execution
- Fastest option for hands-off investigations
- Current behavior, no changes needed

### Mode 2: Coinvestigate
- Job pauses after each iteration for scientist review
- Scientist can provide feedback or just continue
- Auto-continues after 15-minute timeout if no response
- For engaged, interactive investigations

## Coinvestigate Mode Details

### User Experience

After each iteration completes:
1. Job status changes to `awaiting_feedback`
2. UI shows:
   - Current iteration summary and findings
   - Text area for feedback/guidance
   - Countdown timer (15 minutes)
   - Three buttons:
     - **"Submit & Continue"** - Submit feedback and proceed
     - **"Continue"** - Proceed without feedback
     - **"I need more time"** - Reset timer to 15 minutes

3. When timer expires: auto-continue to next iteration (no feedback injected)

### Queue Blocking Solution

**Problem:** "I need more time" button could let a user block the entire job queue indefinitely.

**Solution:** Jobs in `awaiting_feedback` status **do not count** against the concurrent job limit.

This means:
- If max_concurrent = 1 and a job is awaiting feedback, another job can start
- The scientist gets unlimited time to think without blocking others
- When they continue, their job resumes (may need to wait if queue is full)

**Implementation:** In `job_manager.py`, `_get_running_count()` should exclude `awaiting_feedback` jobs.

### Storage

Add to `knowledge_graph.json`:
```json
{
  "feedback_history": [
    {
      "iteration": 3,
      "text": "Focus on metabolic pathways, ignore the gene family analysis",
      "submitted_at": "2024-12-10T15:30:00",
      "delivered": true
    }
  ],
  "pending_feedback": {
    "text": "...",
    "submitted_at": "2024-12-10T15:45:00"
  }
}
```

### Job Status Additions

Add new status to `JobStatus` enum:
- `AWAITING_FEEDBACK` - Job paused, waiting for scientist input

### Orchestrator Changes

1. After each iteration completes (for coinvestigate jobs):
   - Set status to `awaiting_feedback`
   - Record `awaiting_feedback_since` timestamp
   - Wait in a polling loop checking for:
     - Feedback submitted (continue with feedback)
     - Continue clicked (continue without feedback)
     - Timeout expired (auto-continue)

2. At start of each iteration:
   - Check for pending feedback
   - If exists, inject into iteration prompt
   - Mark as delivered

### Prompt Injection Format

```
# Iteration 5/10

## Scientist Feedback
The scientist has provided the following guidance:
> Focus on metabolic pathways, ignore the gene family analysis

Please incorporate this feedback into your investigation approach.

---

[rest of iteration prompt]
```

### Web UI Changes

**Simplify to 2 tabs** (from current 3):
1. **Timeline** - Primary view with everything
2. **Report** - Final markdown/PDF

**New Job Page (`/new`):**
- Add toggle: "Investigation Mode"
  - Autonomous (default) - "Run without pausing"
  - Co-investigate - "Pause after each iteration for my input"

**Job Detail Page (`/job/{id}`) - Timeline Tab:**
- Stats bar at top (status, progress, papers reviewed)
- Research question
- Iteration accordions, each showing:
  - Agent summary as header
  - Expand: analyses, plots, literature, **findings discovered this iteration**
- Findings appear in context of their iteration (not separate summary)

- When status is `awaiting_feedback`:
  - Feedback panel appears at **bottom of timeline** (below last iteration)
  - Shows: "Iteration X complete - awaiting your input"
  - Text area for guidance
  - Countdown timer display
  - Three buttons: "Submit & Continue", "Continue", "I need more time"

- For all coinvestigate jobs:
  - Show feedback history inline in Timeline (which iterations had feedback)
  - Indicate when feedback was injected

## Implementation Plan

### Phase 1: Core Infrastructure
- [ ] Add `AWAITING_FEEDBACK` to JobStatus enum
- [ ] Add `investigation_mode` to job config (autonomous/coinvestigate)
- [ ] Update job_manager to exclude awaiting_feedback from running count
- [ ] Add feedback fields to knowledge graph schema

### Phase 2: Orchestrator
- [ ] Add feedback waiting loop after each iteration (coinvestigate only)
- [ ] Implement timeout logic with timestamp checking
- [ ] Add feedback injection to iteration prompts
- [ ] Handle continue/submit signals from web UI

### Phase 3: Web UI
- [ ] Add investigation mode toggle to new job page
- [ ] Add feedback panel to job detail page (shown when awaiting)
- [ ] Implement countdown timer display
- [ ] Add feedback history to Timeline tab

### Phase 4: Testing
- [ ] Test autonomous mode (should be unchanged)
- [ ] Test coinvestigate mode end-to-end
- [ ] Test timeout auto-continue
- [ ] Test queue not blocked during awaiting_feedback

## Open Questions (Resolved)

1. ~~Should feedback persist across iterations or be one-shot?~~
   → One-shot, but history is preserved for reference

2. ~~Should we show feedback history in the Timeline view?~~
   → Yes, as part of iteration details

3. ~~Should we allow feedback on completed jobs?~~
   → Not in v1, could add later for notes/annotations

4. ~~Should there be email/notification when job completes?~~
   → Not in v1, future enhancement
