# Deployment changes — agent abstraction refactor

Running checklist of deployment-side actions required during or after each PR in the abstraction sequence. See `docs/AGENT_ABSTRACTION_PRS.md` for the per-PR engineering plan.

Append new items as PRs land. Status legend: `[ ]` planned, `[~]` in flight, `[x]` shipped to main.

## Environment variables

### `[x]` PR 1 — `CLAUDE_PROVIDER` -> `OPENSCIENTIST_PROVIDER`

`CLAUDE_PROVIDER` is no longer accepted. The app raises at startup if it is set in the environment. Replace with `OPENSCIENTIST_PROVIDER` in every deployment config, container definition, secrets store, and `.env` file. The accepted values are unchanged.

### `[x]` PR 2 — `ANTHROPIC_MODEL` -> `OPENSCIENTIST_MODEL`

Same shape as above. `ANTHROPIC_MODEL` is rejected at startup, replace with `OPENSCIENTIST_MODEL`. The accepted model id format is unchanged.

### `[ ]` PR 30 — back-compat alias removal

The PR plan reserves a future cleanup PR for dropping any remaining transitional env var aliases. If we add temporary aliases between now and then, list them here when they are added so PR 30 can remove all of them at once.

## On-disk job artefacts

### `[x]` PR 8 — typed `TranscriptEntry` JSON on disk

`iter*_transcript.json` and `report_transcript.json` written by current code are typed `TranscriptEntry` JSON. Job dirs created before PR 8 (raw SDK dict shape) are not readable by the webapp or by `load_transcript` until they are migrated. The webapp report view still renders correctly because it reads `final_report.md`, not the transcript files.

### `[~]` PR 8b — legacy transcript migration script

Run once on every deployment that has pre-PR-8 job dirs:

```
uv run python tools/migrate_legacy_transcripts.py --jobs-dir /path/to/jobs
```

Pass `--dry-run` first to preview the file list. The script is idempotent and safe to re-run. Each original is preserved alongside as `<name>.legacy.json`. Once verified, the `.legacy.json` siblings can be deleted by hand.

The migrator only knows the Claude raw shape. If a future deployment ran a non-Claude backend before PR 8 landed (it did not, in practice, since `SDKAgentExecutor` was the only executor), those files will be left as `unrecognised` in the migrator's summary.

## Process model

### `[x]` Phase 3 (PR 9 to PR 14) — MCP tools become a subprocess

The agent's tools now run in a standalone MCP server spawned as a child of the agent process. Concrete behaviour:

- The agent process invokes `python -m openscientist_tools` via `claude-agent-sdk`'s stdio client. There is no long-running sidecar and no compose service to add.
- Subprocess env is built inside `SDKAgentExecutor._build_subprocess_env()`. It inherits the agent container env (`PATH`, `DATABASE_URL`, `OPENSCIENTIST_SECRET_KEY`, `PHENIX_PATH`, `OPENSCIENTIST_EXECUTOR_*`) and adds per-job overlays (`OPENSCIENTIST_JOB_ID`, `OPENSCIENTIST_JOB_DIR`, optionally `OPENSCIENTIST_DATA_FILE` and `OPENSCIENTIST_DATA_FILES`).
- No new operator-set env vars or compose changes. The agent image already ships `openscientist_tools` because it is part of the same Python package distribution.
- Logs from the MCP subprocess surface through the SDK's `stderr` callback into `SDKAgentExecutor._stderr_lines` and are returned in `IterationResult.error` on iteration failure.
- The MCP subprocess inherits the agent container's docker socket access (the `group_add=[docker_gid]` set by `JobContainerRunner`), so its `execute_code` tool can spawn `openscientist-executor` containers exactly as the in-process path did.
- Rollback path is `git revert` of the cutover PR. The standalone server and the in-process tool modules coexist in the codebase through the cleanup PR that follows, so reverts do not require restoring deleted code.

## Backend selection

### `[ ]` Phase 4 (PR 15 to PR 20) — abstract `AgentExecutor` family

No deployment change expected. The provider selection surface is identical; only the internal class hierarchy changes.

### `[ ]` Phase 5 (PR 21 to PR 29) — Codex backend

Adds a new provider option. Deployment impact:

- The `codex` CLI binary must be present on the agent container's `PATH` (or be installed via a known package).
- New provider config keys (TBD) get a row in the `Environment Variables` table.
- The MCP server registration for the toy fixture (`codex_test_mcp_server.py`) stays scoped to local development and is not required in production.

## How to extend this document

When a future PR introduces a deployment-visible change (new env var, on-disk format shift, new sidecar process, new external dependency, migration step, anything an operator has to do at release time), add a subsection under the most appropriate heading above. Match the style of the existing entries. Keep each entry to one short paragraph plus a code block when there is a command to run.

If a category has no entries yet (Process model, Backend selection, ...) leave the heading in place so future contributors know where to put things.
