# Agent Abstraction Refactor — Migration Record

**Status:** Historical. The refactor is complete and shipped.
**Last updated:** August 2026

This document is the record of the deployment-visible changes made during the agent abstraction refactor, which replaced the original Claude-Code-only executor with a backend-agnostic agent layer supporting both Claude Code and Codex.

It is kept for two reasons: to explain why certain configuration was renamed, and because **one migration step is still operationally relevant** for deployments that predate the typed transcript format (see [Legacy transcript migration](#legacy-transcript-migration)).

For how the system works now, use the current authoritative documents rather than this one:

- Architecture, agent abstraction, providers, MCP: [`DESIGN.md`](DESIGN.md)
- Deployment, configuration, operations: [`DEPLOYMENT.md`](DEPLOYMENT.md)
- Environments and promotion: [`ENVIRONMENTS.md`](ENVIRONMENTS.md)
- Full configuration reference: [`.env.example`](../.env.example)

---

## Still Operationally Relevant

### Legacy transcript migration

Transcripts on disk (`iter*_transcript.json`, `report_transcript.json`) are typed `TranscriptEntry` JSON. Job directories created before that format landed hold the older raw SDK dict shape and are **not readable by the webapp or by `load_transcript`** until migrated. The report view still renders for those jobs, because it reads `final_report.md` rather than the transcript files.

Run once on any deployment that still has pre-migration job directories:

```bash
uv run python tools/migrate_legacy_transcripts.py --jobs-dir /path/to/jobs
```

Pass `--dry-run` first to preview the file list. The script is idempotent and safe to re-run. Each original is preserved alongside as `<name>.legacy.json`; once you have verified the result, the `.legacy.json` siblings can be deleted by hand.

The migrator only understands the Claude raw shape. In practice this is sufficient, because the Claude backend was the only one in existence before the typed format landed. Anything it cannot interpret is reported as `unrecognised` in its summary rather than being modified.

### Renamed environment variables

Both renames are enforced, not advisory: the application **raises at startup** if the old name is present in the environment.

| Old name | Current name | Notes |
|---|---|---|
| `CLAUDE_PROVIDER` | `OPENSCIENTIST_PROVIDER` | Accepted values unchanged. Required; no default. |
| `ANTHROPIC_MODEL` | `OPENSCIENTIST_MODEL` | Accepted model id format unchanged. Optional. |

If you are bringing an old deployment forward, update every deployment config, container definition, secrets store, and `.env` file. There are no remaining transitional aliases to clean up.

---

## Completed Migration Phases

Recorded for context. None of these require operator action today.

### MCP tools became a subprocess

Agent tools moved out of the agent process into a standalone MCP server spawned as a child process. The agent invokes `python -m openscientist_tools` over stdio — there is no long-running sidecar and no Compose service to add. The agent image already ships `openscientist_tools`, since it is part of the same Python distribution.

This required no new operator-set environment variables and no Compose changes. Two properties were important at the time and remain true:

- The subprocess inherits the agent container's `DOCKER_HOST`, which `JobContainerRunner` points at the restricted `docker-socket-proxy`. So `execute_code` can still spawn executor containers without any container holding raw host-socket access.
- MCP subprocess logs surface through the agent's stderr handling and are returned in the iteration result on failure.

Current detail is in [`DESIGN.md`](DESIGN.md); the environment-assembly helper is now `ClaudeCodeAgent._build_subprocess_env()` (the original `SDKAgentExecutor` class no longer exists).

### Abstract agent family

The single concrete executor was replaced by `AbstractAgent` (`src/openscientist/agent/base.py`) with an `AgentBackend` enum and per-backend implementations. As anticipated, this carried **no deployment change**: the provider selection surface was unchanged and only the internal class hierarchy moved.

### Codex backend

A second agent backend shipped alongside Claude Code, adding the `openai`, `azure-openai`, and `ollama` provider options. Deployment impact, all now handled in-tree:

- The `codex` CLI must be on the agent container's `PATH`. `Dockerfile.agent` builds the codex-cli fork from source in a dedicated Rust build stage and installs the binary to `/usr/local/bin/codex`, where `CodexAgent` resolves it.
- The new provider credential keys are documented in [`.env.example`](../.env.example) alongside every other provider.
- `tools/codex_test_mcp_server.py` remains a local development fixture and is not used in production.

---

## Note on References

Earlier revisions of this document referenced a companion `docs/AGENT_ABSTRACTION_PRS.md` containing the per-PR engineering plan. That document is not present in the repository, and the plan it tracked is complete; the reference has been removed rather than repaired.

This document is no longer a running checklist. New deployment-visible changes belong in [`DEPLOYMENT.md`](DEPLOYMENT.md), [`ENVIRONMENTS.md`](ENVIRONMENTS.md), or [`MAINTENANCE.md`](MAINTENANCE.md) as appropriate, not appended here.
