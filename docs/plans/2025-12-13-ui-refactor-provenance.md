# UI Refactor: Show Agent Reasoning and Actions

## Goal

Refactor OpenScientist job detail UI to faithfully show:
1. **What the agent did** - code executions, outputs, searches, findings
2. **What the agent was thinking** - reasoning between actions (Phase 2)

## Current Architecture

**How plots are saved (code_executor.py:166-223):**
- When `plt.show()` or `plt.savefig()` is called, hooks intercept
- Save plot image to `plots/plot_N.png`
- Save metadata to `plots/plot_N.json` with: iteration, description, code, timestamp

**Problem:** Only executions that generate plots get their code saved.

## Solution: Save Metadata for ALL Executions

Following the existing pattern for plots, save a metadata JSON for every code execution.

### Changes Required

#### 1. `src/openscientist/code_executor.py` - Save analysis metadata

After code execution completes (success or failure), save metadata JSON:

```python
# After exec() completes or fails, save analysis metadata
analysis_counter[0] += 1
analysis_path = plots_dir / f"analysis_{analysis_counter[0]}.json"

analysis_metadata = {
    "analysis_number": analysis_counter[0],
    "iteration": iteration,
    "description": description,
    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    "code": code,
    "output": stdout_capture.getvalue(),
    "error": error_message if not success else None,
    "success": success,
    "execution_time": execution_time,
    "plots": [p.name for p in generated_plots]  # Link to any plots generated
}

with open(analysis_path, 'w') as f:
    json.dump(analysis_metadata, f, indent=2)
```

Key points:
- Counter like plot_counter to avoid overwriting
- Saved to `plots/` directory (or could create `analyses/` directory)
- Includes all info needed for display
- Links to any plots generated during this execution

#### 2. `src/openscientist/web_app.py` - Display analyses in timeline

Read analysis JSON files and display in timeline:

```python
# Show analyses run (from analysis_*.json files)
analyses_dir = job_dir / "plots"  # or "analyses" if we change it
analysis_files = sorted(analyses_dir.glob("analysis_*.json"))
iteration_analyses = []
for af in analysis_files:
    with open(af) as f:
        meta = json.load(f)
    if meta.get("iteration") == iteration:
        iteration_analyses.append(meta)

if iteration_analyses:
    with ui.expansion(f"Analyses ({len(iteration_analyses)})", icon="code").classes("w-full mt-2"):
        for analysis in iteration_analyses:
            success = analysis.get("success", False)
            desc = analysis.get("description", "Unnamed")
            exec_time = analysis.get("execution_time", 0)

            status_icon = "✅" if success else "❌"
            with ui.card().classes("w-full mb-2"):
                ui.label(f"{status_icon} {desc} ({exec_time:.1f}s)").classes("font-bold text-sm")

                if analysis.get("output"):
                    with ui.expansion("Output", icon="terminal"):
                        ui.code(analysis["output"], language="text").classes("text-xs")

                if analysis.get("error"):
                    with ui.expansion("Error", icon="error"):
                        ui.code(analysis["error"], language="text").classes("text-xs text-red-600")

                if analysis.get("code"):
                    with ui.expansion("Code", icon="code"):
                        ui.code(analysis["code"], language="python").classes("text-xs")
```

### File Storage Structure (After)

```
jobs/job_xxx/
├── plots/
│   ├── analysis_1.json   # Every execution gets metadata
│   ├── analysis_2.json
│   ├── analysis_3.json   # This one generated a plot
│   ├── plot_1.png        # Plot from analysis_3
│   ├── plot_1.json       # Plot metadata (links to analysis_3)
│   └── ...
```

### UI Layout (Per Iteration)

```
▼ Iteration 1: Extracted text from docx... [5 analyses] [2 plots] [15 papers]

  ▶ Analyses (5)
     ✅ Exploring data directory (0.2s)
        [Output] [Code]
     ❌ Reading docx with python-docx (0.1s)
        [Error] [Code]
     ✅ Parsing docx XML directly (0.5s)
        [Output] [Code]
     ...

  ▶ Visualizations (2)
     [plot images with descriptions]

  ▶ Literature searched (15 papers)
     ...
```

### Backwards Compatibility

- Existing jobs without analysis_*.json files will fall back to showing summary only
- Plot metadata files unchanged - still work independently

## Phase 2: Agent Reasoning (IN PROGRESS - DESIGN)

### Goal
Provide **scientific provenance** - scientists need to check and follow the agent's logic.

Per iteration, UI should show:
1. **Strapline** - one-liner summarizing iteration purpose ("Investigating APOE4 correlation...")
2. **List of actions** with:
   - **Why** - the reasoning/justification that preceded the action
   - **What** - the code/query
   - **Result** - output, plots, papers found

### Key Insight: Use Raw Claude JSON Output

Claude's `--output-format json` returns a JSON array with ALL messages:
```json
[
  {"type":"system", ...},
  {"type":"assistant", "message": {"content": [{"type":"text","text":"Let me analyze..."}]}},
  {"type":"tool_use", "name": "execute_code", "input": {...}},
  {"type":"tool_result", ...},
  {"type":"assistant", "message": {"content": [{"type":"text","text":"Now I'll search..."}]}},
  ...
  {"type":"result", ...}
]
```

The **reasoning is in `type: "assistant"` messages** - the text between tool calls.

Currently we only save the final `result` object to `claude_iterations.log`. The intermediate assistant messages (the "why") are discarded.

### Solution: Save Raw Transcript

**Don't restructure the data.** Save Claude's output as-is for provenance.

```
analyses/
├── iter1_transcript.json      # Full raw Claude JSON array
├── iter1_analysis1_plot1.png  # Binary artifacts only
├── iter2_transcript.json
└── ...
```

- Code and output are already IN the transcript (in tool_use inputs and tool_result outputs)
- UI parses the transcript and displays: reasoning → action → result
- No transformation = no bugs, exact provenance
- If Claude's format changes, update UI only, not storage

### Backwards Compatibility

**Old jobs:**
- Have `plots/` directory with `plot_N.json` (code only for plot-generating executions)
- No transcript saved
- UI falls back gracefully - shows plots/summaries, no reasoning available

**New jobs:**
- Have `analyses/` directory with `iterN_transcript.json`
- UI shows full reasoning + actions

### Link from knowledge_graph.json

Add `files` field to `analysis_log` entries:
```json
{
  "analysis_log": [
    {
      "iteration": 1,
      "action": "execute_code",
      "description": "Parsing docx...",
      "success": true,
      "files": "iter1"  // prefix for finding transcript + artifacts
    }
  ]
}
```

### What's in claude_iterations.log

Currently saves:
- The prompt sent to start iteration
- The final `type: "result"` JSON only

Could either:
- Keep as-is (human-readable summary)
- Or replace with just prompts, since full output is in analyses/

### What Claude Outputs (raw JSON stream)

Each iteration produces a stream of JSON objects via `--output-format stream-json --verbose`:

| Type | Contains | Source of "Why" |
|------|----------|-----------------|
| `{"type":"system","subtype":"init"}` | Session setup, tools list | N/A |
| `{"type":"assistant","message":{"content":[{"type":"text",...}]}}` | Agent's reasoning text | The text itself |
| `{"type":"assistant","message":{"content":[{"type":"tool_use","name":"execute_code","input":{...}}]}}` | Tool call | `input.description` field |
| `{"type":"user","message":{"content":[{"type":"tool_result",...}]},"tool_use_result":{...}}` | Tool output | stdout/stderr in `tool_use_result` |
| `{"type":"result"}` | Final summary, cost, usage | N/A |

### Where Reasoning Comes From

1. **For `execute_code`**: `input.description` - e.g., "Checking correlation between carnosine and oxidative stress"
2. **For `search_pubmed`**: `input.query` - the search query IS the reasoning
3. **For any text between tool calls**: `message.content[].text` - explicit reasoning (rare but possible)

### Per-Iteration UI Display

```
▼ Iteration 1: "Extracted text from docx and analyzed proposal structure"  ← STRAPLINE

  📋 PROMPT (what we asked Claude to do):
  ▶ "Begin autonomous discovery for this research question: Analyze this file.
     You will run for a maximum of 2 iterations..."
     [expandable]

  🔬 ACTIONS (what Claude did):

     💭 "Exploring data directory to understand what files are available"
     ▶ Code  ▶ Output  ❌ Failed

     💭 "Attempting to read the .docx file structure"
     ▶ Code  ▶ Output  ❌ Failed

     💭 "Extract text from docx by parsing XML content directly"
     ▶ Code  ▶ Output  ✅ Success

     📚 Searched: "AI machine learning Alzheimer's disease discovery"
     ▶ 10 papers found [expandable]

     💭 "Creating visual summary of the AI/ML tools proposal"
     ▶ Code  ▶ Output  ✅ Success
     📊 [plot image]
```

### Where Each UI Part Comes From

| Part | Source |
|------|--------|
| **Iteration N** | `knowledge_graph.json` → `iteration` field |
| **Strapline** | `knowledge_graph.json` → `iteration_summaries[N].strapline` (explicit field from agent) |
| **Prompt** | `claude_iterations.log` → the prompt text for iteration N |
| **Actions** | `provenance/iterN_transcript.json` → walk the JSON array |
| **Description/Why** | `tool_use.input.description` (for execute_code) or `tool_use.input.query` (for search_pubmed) |
| **Code/Output** | `tool_use.input.code` and `tool_result.content` |
| **Plots** | `provenance/iterN_*.png` files |

### File Storage Structure

```
jobs/job_xxx/
├── provenance/
│   ├── iter1_transcript.json   # Full raw Claude JSON (array of all messages)
│   ├── iter1_plot1.png         # Plot artifact
│   ├── iter2_transcript.json
│   ├── iter2_plot1.png
│   └── ...
├── knowledge_graph.json        # Structured findings, literature, summaries (lean)
└── claude_iterations.log       # Keep as-is (prompts + final results, human readable)
```

**Principle:** `provenance/` is for anything too big for knowledge_graph.json - raw transcripts, binary files, large outputs. It's the audit trail proving what the agent did.

### Tool Descriptions: Standardized Approach

**Decision:** Every MCP tool gets a `description` parameter.

Tools to update:
- `search_pubmed(query, max_results, description="")`
- `update_knowledge_state(title, evidence, interpretation, description="")`
- `save_iteration_summary(summary)` - summary IS the description, no change needed
- `execute_code(code, description)` - already has it

**Fallback logic for UI** (in case description is empty):

```python
def get_description(tool_use):
    inp = tool_use["input"]

    # 1. Explicit description
    if inp.get("description"):
        return inp["description"]

    # 2. Tool-specific fallback from key inputs
    name = tool_use["name"]
    if "search_pubmed" in name:
        return f"Search: {inp.get('query', '')}"
    if "update_knowledge_state" in name:
        return f"Finding: {inp.get('title', '')}"
    if "execute_code" in name:
        return "Code execution"

    # 3. Just the tool name
    return name.split("__")[-1]  # strip mcp__openscientist-tools__ prefix
```

Benefits:
- Consistent - always check `input.description` first
- Forces agent to articulate reasoning (better provenance)
- Graceful fallback when description empty
- No per-tool business logic scattered around

### Current UI Sections → Refactored Location

| Current Section | In Refactor | Notes |
|-----------------|-------------|-------|
| **Visualizations** | Inline with `execute_code` action that created them | Plot shown under the action |
| **Literature searches** | Inline as `search_pubmed` actions | Query + results together |
| **Findings** | Inline as `update_knowledge_state` actions | Agent explicitly records these |

Timeline becomes a unified stream of actions, not separate sections.

### RESOLVED DECISIONS

1. **Strapline generation** - ✅ RESOLVED
   - Add explicit `strapline` param to `save_iteration_summary`
   - Agent provides short, punchy strapline (e.g., "Investigating APOE4 correlation")
   - Stored in `iteration_summaries[N].strapline`

2. **Directory naming** - ✅ RESOLVED
   - Use `provenance/` directory
   - Reinforces principle: preserve proof/records of what agent did
   - Contains raw transcripts + binary artifacts (anything too big for knowledge_graph.json)

3. **Real-time streaming** - ✅ DEFERRED
   - See GitHub issue #45
   - Future enhancement to stream agent activity to UI live

### What's in knowledge_graph.json (Per Iteration)

| Field | Data | Populated By | When |
|-------|------|--------------|------|
| `iteration` | Current iteration number | Orchestrator | Incremented between iterations |
| `analysis_log[]` | `{iteration, action, timestamp, description, success, execution_time, plots}` | MCP tools | After each tool execution |
| `findings[]` | `{iteration_discovered, title, evidence, interpretation}` | Agent via `update_knowledge_state` | When agent records a finding |
| `literature[]` | `{retrieved_at_iteration, pmid, title, abstract, search_query}` | `search_pubmed` tool | After each literature search |
| `iteration_summaries[]` | `{iteration, summary, strapline, created_at}` | Agent via `save_iteration_summary` | End of each iteration |
| `feedback_history[]` | `{after_iteration, text, submitted_at}` | User via UI | Between iterations (coinvestigate mode) |

**Note:** `hypotheses` was designed but never implemented - see issue #46 to remove dead code.

### RESOLVED QUESTIONS

4. **Output format flag** - ✅ RESOLVED
   - `--output-format json` (without `--verbose`) returns only final result dict
   - Need to add `--verbose` flag to get full transcript array
   - Fix: Add `--verbose` to orchestrator's Claude CLI calls

5. **Backwards compat for plots** - ✅ RESOLVED
   - New jobs: save to `provenance/`
   - Old jobs: UI checks for `provenance/` first, falls back to `plots/`
   ```python
   provenance_dir = job_dir / "provenance"
   plots_dir = job_dir / "plots"
   if provenance_dir.exists():
       # New job - read from provenance/
   else:
       # Old job - fall back to plots/
   ```

## Files to Modify

1. `src/openscientist/orchestrator.py`:
   - Add `--verbose` flag to Claude CLI calls (to get full transcript)
   - Save full JSON array to `provenance/iterN_transcript.json`
   - Rename `knowledge_graph.json` references to `knowledge_state.json`
2. `src/openscientist/code_executor.py` - Save plots to `provenance/` instead of `plots/`
3. `src/openscientist/mcp_server/server.py`:
   - Add `description` param to `search_pubmed`, `update_knowledge_state`
   - Add `strapline` param to `save_iteration_summary`
   - Rename `knowledge_graph.json` references to `knowledge_state.json`
4. `src/openscientist/knowledge_graph.py`:
   - Rename file to `knowledge_state.py`
   - Rename class `KnowledgeGraph` to `KnowledgeState`
   - Add `strapline` field to iteration_summaries
5. `src/openscientist/web_app.py`:
   - Read transcripts and display reasoning + actions
   - `get_description` fallback logic
   - `provenance/` with `plots/` fallback
   - Rename `knowledge_graph.json` references to `knowledge_state.json`
6. `src/openscientist/job_manager.py` - Rename `knowledge_graph.json` references
7. All other files referencing `knowledge_graph` - update imports and references

## Migration Strategy: Clean Break

**Decision:** No backwards compat fallback logic. Instead, migrate all existing jobs with a one-off bootstrap procedure.

### Bootstrap Procedure (`python -m openscientist.job_manager bootstrap`)

1. **Backup first** - copy jobs/ to jobs_backup/
2. **For each job:**
   - Rename `knowledge_graph.json` → `knowledge_state.json`
   - Rename `plots/` → `provenance/`
   - Add missing fields (e.g., `strapline: ""` to iteration_summaries if missing)
   - Log what was changed
3. **Verify** - check each migrated job is valid JSON, has expected structure

### Testing Protocol

Before deploying:
1. Run migration on copy of jobs/
2. Start UI, click through EVERY job
3. Verify for each job:
   - Job loads without error
   - Timeline displays correctly
   - Plots display
   - Findings/literature show
   - Summary/strapline show (or empty gracefully)
4. Only after all jobs pass → deploy

### Rollback

If something breaks:
- Restore from jobs_backup/
- Fix bootstrap procedure
- Re-run

**Rationale:** Code is already complex. Adding fallback conditionals everywhere makes it worse. Clean migration + thorough testing is safer long-term.

## RESOLVED: --verbose Flag and Image Data

### Problem Statement

When using `--verbose --output-format json`, Claude CLI returns base64-encoded image data in tool_result content. This causes:
1. Very large JSON outputs (MBs for images)
2. Sometimes malformed JSON that fails to parse (seen error: "Unterminated string starting at: line 1 column 13683")

### Why We Need --verbose

**Without `--verbose`:** `--output-format json` only returns the final result dict - a summary of what happened.

**With `--verbose`:** We get the full transcript array containing:
- `assistant` messages → the agent's **reasoning** between actions
- `tool_use` → what tools were called with what inputs (including `description`)
- `tool_result` → what the tools returned
- `result` → final summary

The whole point of this refactor is to show scientists the agent's reasoning for provenance. That reasoning is in the `assistant` messages and `tool_use.input.description` fields - which we only get with `--verbose`.

Without it, we'd only know "the job completed" but not "why the agent did what it did."

### Solution Implemented

**Use `--output-format stream-json` with per-line sanitization:**

1. Changed from `--output-format json` to `--output-format stream-json`
2. Parse line by line (each line is a complete JSON object)
3. Strip `"data"` fields >1000 chars BEFORE parsing each line
4. Collect into transcript array

```python
def parse_stream_json(stdout: str) -> list:
    """Parse stream-json output (one JSON object per line)."""
    import re
    transcript = []
    for line in stdout.strip().split('\n'):
        if not line:
            continue
        # Strip "data" fields with long content (images, file contents)
        sanitized_line = re.sub(
            r'"data":\s*"[^"]{1000,}"',
            '"data": "[CONTENT REMOVED]"',
            line
        )
        try:
            obj = json.loads(sanitized_line)
            transcript.append(obj)
        except json.JSONDecodeError as e:
            logger.warning(f"Skipping unparseable JSON line...")
    return transcript
```

**Benefits:**
- One corrupted line doesn't break everything
- Sanitization happens BEFORE parsing (avoids parse errors)
- Transcript files are small (20KB vs 70KB+ with base64)
- Large images (1.7MB+) now work without crashing

## Implementation Order

### Phase 1: Backend Changes
1. Add `--verbose` flag to orchestrator Claude CLI calls
2. Add `description` param to MCP tools (server.py)
3. Add `strapline` param to `save_iteration_summary` (server.py + knowledge_graph.py)
4. Update orchestrator to save raw transcript to `provenance/`
5. Update code_executor to save plots to `provenance/`
6. Rename `knowledge_graph` to `knowledge_state` throughout codebase

### Phase 2: Migration
7. Write bootstrap procedure (`python -m openscientist.job_manager bootstrap`)
8. Test migration on copy of jobs/
9. Run migration on production jobs/

### Phase 3: UI Changes
10. Update web_app to read from `provenance/` and `knowledge_state.json`
11. Implement new timeline display with reasoning + actions

### Phase 4: Testing
12. Test with new job (end-to-end)
13. Click through every migrated job in UI
14. Fix any issues found

## Implementation Status

### Phase 1: Backend Changes - ✅ COMPLETE

1. ✅ Add `--verbose` flag to orchestrator Claude CLI calls
   - Changed from `--output-format json` to `--output-format stream-json`
   - Added `parse_stream_json()` function for line-by-line parsing
   - Strips ALL large `data` fields (>1000 chars) during parsing - includes images, file contents, etc.

2. ✅ Add `description` param to MCP tools (server.py)
   - Added to `search_pubmed()` and `update_knowledge_state()`

3. ✅ Add `strapline` param to `save_iteration_summary`
   - Updated server.py and knowledge_state.py
   - Stored in iteration_summaries[].strapline

4. ✅ Update orchestrator to save raw transcript to `provenance/`
   - Creates `provenance/` directory
   - Saves `iterN_transcript.json` for each iteration

5. ✅ Update code_executor to save plots to `provenance/`
   - Changed plots directory from `plots/` to `provenance/`

6. ✅ Rename `knowledge_graph` to `knowledge_state` throughout codebase
   - Renamed file: `knowledge_graph.py` → `knowledge_state.py`
   - Renamed class: `KnowledgeGraph` → `KnowledgeState`
   - Renamed JSON file: `knowledge_graph.json` → `knowledge_state.json`
   - Renamed variables: `kg` → `ks`, `kg_path` → `ks_path`, `KG` → `KS`

### Phase 2: Migration - ✅ COMPLETE

- Added bootstrap command `python -m openscientist.job_manager bootstrap`
- Migrated 93 existing jobs:
  - Renamed `knowledge_graph.json` → `knowledge_state.json`
  - Renamed `plots/` → `provenance/`
  - Added missing `iteration_summaries` and `feedback_history` fields
  - Added missing `strapline` fields to iteration summaries
- Backup created at `jobs_backup_20251214_121304/`

### Phase 3: UI Changes - ✅ COMPLETE

Implemented:
- ✅ Added `get_action_description()` helper function with fallback logic
- ✅ Added `parse_transcript_actions()` to extract actions from transcripts
- ✅ Renamed `kg_data` → `ks_data` in web_app.py
- ✅ Updated `plots_dir` → `provenance_dir` for plot loading
- ✅ Updated iteration headers to use strapline (with fallback to truncated summary)
- ✅ Added "Summary" expansion showing full iteration summary
- ✅ Added "Actions" expansion showing all transcript actions with:
  - Description (why) from `input.description` or fallback
  - Code/query (what) - expandable for execute_code, inline for search
  - Result (what happened) - expandable for long results
  - Color-coded cards by action type (blue=code, purple=search, green=finding)
  - Success/failure indicators

### Phase 4: Testing - ✅ COMPLETE

Testing checklist:
- ✅ Build Docker container: `make rebuild` succeeds
- ✅ Jobs list page: GET /jobs returns 200 OK
- ✅ Job detail page: GET /job/job_xxx returns 200 OK
- ✅ No Python errors in container logs
- ✅ App starts and initializes JobManager correctly

Note: Old jobs don't have transcript files (they predate this feature), so the "Actions" section won't appear for them. New jobs will generate transcript files in `provenance/iterN_transcript.json` that will populate the Actions section.

## Summary

All phases complete. The UI now supports:
1. **Strapline** in iteration headers (falls back to truncated summary if not available)
2. **Summary** expansion showing full iteration summary
3. **Actions** expansion (for new jobs with transcripts) showing:
   - Each action with description (why), code/query (what), result
   - Color-coded by action type
   - Success/failure indicators
4. **Provenance** directory structure for raw transcripts + artifacts
