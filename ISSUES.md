# Known Issues

## MCP Tools Not Being Used (Critical)

**Status:** Under investigation
**Priority:** High
**Date:** 2025-11-14

### Problem

Claude Code CLI is not using the custom MCP tools (`execute_code`, `search_pubmed`, `update_knowledge_graph`) despite:
- MCP server being implemented
- `--mcp-config` flag being passed to CLI
- MCP config file being correctly formatted

### Evidence

1. Job iteration logs show Claude successfully analyzes data and finds results (Carnosine +38%, Anserine +48%, etc.)
2. BUT: All tabs (findings/hypotheses/literature/analysis log) are blank in the web UI
3. Claude tries to use `Bash` and `WebSearch` instead of MCP tools (permission_denials in logs)
4. MCP server responds correctly to manual JSON-RPC requests
5. Claude Code CLI `mcp list` shows "Failed to connect" even after adding `initialize` method

### Root Cause Investigation

**What we've tried:**
- ✅ Added missing `__main__.py` to make server executable
- ✅ Added `initialize()` method for MCP protocol handshake
- ✅ Fixed absolute paths in MCP config
- ✅ Verified MCP server responds to manual JSON-RPC calls
- ❌ Still failing: Claude Code CLI cannot connect to stdio MCP server

**Hypothesis:**
The stdio-based MCP server may have additional protocol requirements beyond `initialize` and `tools/list`. Claude Code CLI's "Failed to connect" suggests the handshake isn't completing.

### Design Question

**Do we even need a custom MCP server?**

The original design uses MCP tools to:
1. `execute_code` - Run Python with pre-loaded DataFrame
2. `search_pubmed` - Search scientific literature
3. `update_knowledge_graph` - Record findings to JSON file

But Claude Code CLI already has:
1. `Bash` - Can run Python scripts directly
2. `Read`/`Write`/`Edit` - Can update knowledge_graph.json
3. `WebSearch` - Can search web (or use Python requests for PubMed API)

**Alternative approach:**
Remove custom MCP server, use built-in tools + explicit instructions in CLAUDE.md:
- Use Bash to run Python for analysis (with job dir in PYTHONPATH or explicit imports)
- Use Write/Edit to update knowledge_graph.json with findings
- Use WebSearch or Bash+Python for PubMed queries

This would:
- ✅ Eliminate MCP connection issues
- ✅ Work with standard Claude Code CLI
- ✅ Be simpler to maintain
- ❌ Less encapsulation (no single tool for "execute_code")
- ❌ Requires more explicit instructions in prompts

### Research Findings (2025-11-14)

#### 1. How Others Build MCP Servers

**Key Discovery:** The official approach is to use the **FastMCP Python SDK**, NOT manual JSON-RPC implementation.

From `github.com/modelcontextprotocol/python-sdk`:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("ServerName")

@mcp.tool()
def my_tool(param: str) -> str:
    """Tool description"""
    return f"Result: {param}"
```

**What FastMCP provides:**
- ✅ Automatic MCP protocol compliance (initialize, tools/list, etc.)
- ✅ Handles all JSON-RPC messaging
- ✅ Manages stdio connection
- ✅ No manual protocol implementation needed

**Our mistake:** We manually implemented JSON-RPC and are missing protocol requirements that FastMCP handles automatically.

#### 2. Scientific Computing with Claude Code

**Finding:** No examples of MCP servers for scientific computing/data analysis found in official docs.

Claude Code examples focus on:
- Business tools (Notion, Airtable, Asana)
- All examples use either HTTP endpoints or NPM packages (npx)
- No Python stdio server examples shown

**Implication:** We may be pioneers in using MCP for scientific discovery workflows, OR this isn't the typical approach.

#### 3. Common Patterns

Most Claude Code users appear to use:
- **Built-in tools** (Bash, Read, Write, Edit) for custom workflows
- **HTTP MCP servers** for remote services
- **NPM-based servers** (npx packages) for local tools

**Recommendation:** Consider either:
- A) Rewrite using FastMCP SDK (proper MCP implementation)
- B) Abandon MCP entirely, use built-in Bash + file tools (simpler)

### Proposed Solutions

#### Option A: Rewrite Using FastMCP SDK (Proper MCP)

**Pros:**
- ✅ Official, supported approach
- ✅ Automatic protocol compliance
- ✅ Clean tool abstraction
- ✅ May work out-of-the-box once we use the SDK

**Cons:**
- ⏱️ Requires rewriting server.py
- 📦 New dependency (`mcp[cli]`)
- 🤔 Still unproven for scientific workflows
- 📚 Need to learn FastMCP API

**Implementation:**
```python
from mcp.server.fastmcp import FastMCP
import pandas as pd

mcp = FastMCP("shandy-tools")

# Load data at server init
data = pd.read_csv(DATA_FILE)

@mcp.tool()
def execute_code(code: str) -> str:
    """Execute Python code with data DataFrame available"""
    # Execute in namespace with data
    ...

@mcp.tool()
def search_pubmed(query: str, max_results: int = 10) -> list:
    """Search PubMed for papers"""
    ...

@mcp.tool()
def update_knowledge_graph(title: str, evidence: str, interpretation: str = "") -> str:
    """Record finding to knowledge graph"""
    ...
```

#### Option B: Use Built-in Tools (No MCP)

**Pros:**
- ✅ Works immediately, no debugging
- ✅ No new dependencies
- ✅ Proven approach (Claude successfully analyzed data)
- ✅ Simpler architecture

**Cons:**
- ❌ Less encapsulation
- ❌ More verbose prompts
- ❌ Need Python helper scripts for common operations
- ❌ Claude needs explicit instructions to update knowledge_graph.json

**Implementation:**
1. Create `src/shandy/helpers/analyze.py` with reusable analysis functions
2. Update CLAUDE.md to instruct using Bash + helpers
3. Provide example code for updating knowledge_graph.json
4. Remove MCP server code entirely

Example prompt structure:
```
To analyze data:
1. Use Bash: python -c "import pandas as pd; data = pd.read_csv('path'); ..."
2. To record findings: Use Edit tool on knowledge_graph.json
3. To search PubMed: Use Bash with Python requests
```

#### Option C: Hybrid Approach

**Keep MCP for what it's good at**, use built-in tools for the rest:
- MCP: Complex multi-step operations (search_pubmed with parsing)
- Bash: Quick data analysis
- Edit: Update knowledge graph directly

### Next Steps

- [ ] **Decision needed:** Choose Option A, B, or C
- [ ] If A: Install FastMCP, rewrite server.py, test
- [ ] If B: Create helper scripts, update CLAUDE.md, remove MCP code
- [ ] If C: Determine which operations warrant MCP vs built-in tools
- [ ] Test chosen approach with a small job
- [ ] Document the decision and rationale

### Related Files

- `src/shandy/mcp_server/server.py` - Custom MCP server implementation
- `src/shandy/orchestrator.py` - Creates MCP config, passes to Claude CLI
- `jobs/*/mcp_config.json` - Per-job MCP configuration
- `jobs/*/claude_iterations.log` - Shows permission_denials for Bash/WebSearch
- `jobs/*/knowledge_graph.json` - Should contain findings but is empty

### Workaround

For now, Claude is successfully analyzing data (visible in final_report.md), but findings aren't being logged to the knowledge graph for display in the web UI. The science is working, just not the structured data capture.
