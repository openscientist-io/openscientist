# UI Refactor: Show Agent Reasoning and Actions

## Goal

Refactor SHANDY job detail UI to faithfully show:
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

#### 1. `src/shandy/code_executor.py` - Save analysis metadata

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

#### 2. `src/shandy/web_app.py` - Display analyses in timeline

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
- `update_knowledge_graph(title, evidence, interpretation, description="")`
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
    if "update_knowledge_graph" in name:
        return f"Finding: {inp.get('title', '')}"
    if "execute_code" in name:
        return "Code execution"

    # 3. Just the tool name
    return name.split("__")[-1]  # strip mcp__shandy-tools__ prefix
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
| **Findings** | Inline as `update_knowledge_graph` actions | Agent explicitly records these |

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
| `findings[]` | `{iteration_discovered, title, evidence, interpretation}` | Agent via `update_knowledge_graph` | When agent records a finding |
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

1. `src/shandy/orchestrator.py`:
   - Add `--verbose` flag to Claude CLI calls (to get full transcript)
   - Save full JSON array to `provenance/iterN_transcript.json`
   - Rename `knowledge_graph.json` references to `knowledge_state.json`
2. `src/shandy/code_executor.py` - Save plots to `provenance/` instead of `plots/`
3. `src/shandy/mcp_server/server.py`:
   - Add `description` param to `search_pubmed`, `update_knowledge_graph`
   - Add `strapline` param to `save_iteration_summary`
   - Rename `knowledge_graph.json` references to `knowledge_state.json`
4. `src/shandy/knowledge_graph.py`:
   - Rename file to `knowledge_state.py`
   - Rename class `KnowledgeGraph` to `KnowledgeState`
   - Add `strapline` field to iteration_summaries
5. `src/shandy/web_app.py`:
   - Read transcripts and display reasoning + actions
   - `get_description` fallback logic
   - `provenance/` with `plots/` fallback
   - Rename `knowledge_graph.json` references to `knowledge_state.json`
6. `src/shandy/job_manager.py` - Rename `knowledge_graph.json` references
7. All other files referencing `knowledge_graph` - update imports and references

## Migration Strategy: Clean Break

**Decision:** No backwards compat fallback logic. Instead, migrate all existing jobs with a one-off script.

### Migration Script (`scripts/migrate_jobs.py`)

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
- Fix migration script
- Re-run

**Rationale:** Code is already complex. Adding fallback conditionals everywhere makes it worse. Clean migration + thorough testing is safer long-term.

## Implementation Order

### Phase 1: Backend Changes
1. Add `--verbose` flag to orchestrator Claude CLI calls
2. Add `description` param to MCP tools (server.py)
3. Add `strapline` param to `save_iteration_summary` (server.py + knowledge_graph.py)
4. Update orchestrator to save raw transcript to `provenance/`
5. Update code_executor to save plots to `provenance/`
6. Rename `knowledge_graph` to `knowledge_state` throughout codebase

### Phase 2: Migration
7. Write migration script (`scripts/migrate_jobs.py`)
8. Test migration on copy of jobs/
9. Run migration on production jobs/

### Phase 3: UI Changes
10. Update web_app to read from `provenance/` and `knowledge_state.json`
11. Implement new timeline display with reasoning + actions

### Phase 4: Testing
12. Test with new job (end-to-end)
13. Click through every migrated job in UI
14. Fix any issues found
