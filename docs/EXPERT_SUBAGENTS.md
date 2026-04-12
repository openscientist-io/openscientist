<!-- cSpell:words openscientist wshobson voltagent thirdparty frontmatter dataclass docstrings pyyaml pyproject mypy pytest venv varchar timestamptz jsonb OODA bgpt bioinformatics kwarg footgun asyncpg Alembic asynccontextmanager BYPASSRLS CHECK -->

# Expert Subagents

This document describes the **expert subagent** capability added to the
OpenScientist job agent: what it is, why we built it the way we did, what
landed in this PR, and what remains for follow-ups.

It is written as both a PR rationale (for the reviewer who has the diff
in front of them) and a permanent reference (for the future engineer who
needs to understand the system from scratch).

---

## 1. Objectives

The OpenScientist job agent is a Claude Code instance running inside a
per-job Docker container, doing autonomous scientific discovery. Until
this PR it was a single agent talking to a fixed tool surface.

We wanted it to be able to **delegate to specialist expert agents** for
narrow, well-scoped tasks: literature retrieval, statistical methodology
critique, citation grounding, focused research subqueries, and so on.
This mirrors how human research teams work — a lead researcher orchestrates
the investigation and pulls in domain experts when their depth is needed.

Concrete goals going in:

1. **Use authoritative best practices** from Anthropic's own documentation
   for how Claude Code subagents should be defined and invoked.
2. **Vendor existing curated collections** rather than write every expert
   from scratch — there is real public work to lift.
3. **Database-backed storage** so experts are runtime-editable and can
   eventually be managed via an admin UI.
4. **Inherit model from the orchestrator** so experts cost the same per
   token as the parent — explicit user policy decision.
5. **Single-PR delivery** with rigorous test-first discipline.

---

## 2. Architecture

```text
+--------------------------+      +----------------------------+
|  thirdparty/*.md files   |      |  seed migration            |
|  (vendored prompts,      |      |  (embedded frozen literal, |
|   read by runtime helper |      |   no file I/O at upgrade)  |
|   and tests)             |      +-------------+--------------+
+--------------------------+                    |
                                                | alembic upgrade head
                                                v
                                  +------------------------------+
                                  |  experts table               |
                                  |  (Postgres, admin-only RLS)  |
                                  +-------------+----------------+
                                                |
                                                | get_admin_session()
                                                | (ADMIN_DATABASE_URL →
                                                |  openscientist_admin role
                                                |  with BYPASSRLS)
                                                v
                                  +------------------------------+
                                  |  load_enabled_experts(sess)  |
                                  |  (per-row validation with    |
                                  |   warn-and-skip resilience)  |
                                  +-------------+----------------+
                                                |
                                                v
                                  +------------------------------+
                                  |  dict[slug, AgentDefinition] |
                                  +----+--------------------+----+
                                       |                    |
                                       v                    v
                             +----------------+   +-------------------+
                             | run_discovery  |   |  chat builder     |
                             |   async        |   |  (per-message)    |
                             +--------+-------+   +---------+---------+
                                      |                     |
                                      | loads ONCE,         |
                                      | passes to both      |
                                      v                     |
                             +----------------+              |
                             |_write_skills_  |              |
                             | to_claude_dir  |              |
                             +--------+-------+              |
                                      |                     |
                                      | renders             |
                                      | JOB_CLAUDE.md       |
                                      v                     |
                             +----------------+              |
                             |_build_agent_   |<-------------+
                             | executor       |
                             +--------+-------+
                                      |
                                      v
                     +-------------------------------------+
                     |  SDKAgentExecutor(experts=...)      |
                     |    ↓                                |
                     |  ClaudeAgentOptions(agents=...)     |
                     |    ↓                                |
                     |  claude-agent-sdk initialize req    |
                     +-------------------------------------+
```

The single `load_enabled_experts(session)` helper feeds both the discovery
loop's executor builder (`orchestrator/discovery.py:_build_agent_executor`)
and the chat service's executor builder (`job_chat.py:_build_chat_executor`).
Both code paths go through the same `SDKAgentExecutor.__init__(experts=...)`
constructor and the same `_build_options()` step that wires `agents=` into
the SDK's `ClaudeAgentOptions`.

The experts dict is **frozen at session init** by the SDK — there is no
way to add or remove experts mid-conversation. Loading happens once at
executor construction time.

---

## 3. Key design decisions

### 3.1 Programmatic dict, not `.claude/agents/*.md` files

The `claude-agent-sdk` Python package supports two mutually exclusive ways
of registering subagents:

1. **Programmatic** — `ClaudeAgentOptions(agents={"slug": AgentDefinition(...)})`
   sent via the SDK initialize request.
2. **Filesystem** — `.claude/agents/*.md` files in the cwd, loaded only
   when `setting_sources` explicitly includes `'project'`, `'user'`, or
   `'local'`.

The SDK is **hermetic by default**: when `setting_sources=None` (the
default, which the openscientist executor uses), it passes
`--setting-sources ""` to the bundled CLI, which means *no filesystem
sources at all*. This was empirically verified against SDK v0.1.46:

| CLI invocation | `.claude/agents/test.md` loaded? |
|---|---|
| `claude agents list` (no flag, interactive default) | yes |
| `claude agents list --setting-sources ""` (SDK default) | **no** |
| `claude agents list --setting-sources project` | yes |

This means the natural-feeling "drop a markdown file in `.claude/agents/`"
pattern would silently fail under our SDK usage. We chose Path 1
(programmatic) because:

- It pairs cleanly with database storage (DB row → `AgentDefinition`).
- It does not require opting into `setting_sources=['project']`, which
  would also load `.claude/settings.json`, hooks, and plugins from the
  cwd — broader attack surface for an autonomous container.
- It is the single mechanism that does not depend on which directory the
  CLI subprocess starts in.

There is a related subtlety worth recording: **skills work today not
because the SDK loads them, but because the system prompt instructs the
agent to use its `Read` tool on `.claude/skills/*.md`**. Skills are plain
data the agent reads. Subagents cannot work that way because there is no
runtime "register-a-subagent-from-a-file" primitive — the subagent
registry is fixed at session init.

This finding is also captured in the user's auto-memory at
`memory/sdk_subagents_loading.md`.

### 3.2 Database storage, not Python registry

The user explicitly chose database storage over a Python registry. The
trade-off:

| | Python registry | DB table (chosen) |
|---|---|---|
| Edit at runtime | needs deploy | yes |
| Type-checked | yes | weaker (JSONB tools, free-form model) |
| Migration cost | none | one Alembic migration (schema + seed) |
| Admin UI possible | no | yes (deferred) |
| Visible to other services | no | yes (any DB client) |

Storing experts in the DB also makes attribution durable: vendored
third-party prompts carry `source` and `source_url` columns that survive
any serialization. MIT compliance is handled by the vendored frontmatter
comments and the `source_url` pointing at upstream repos.

### 3.3 Global scope, not per-job or per-user

Experts are global catalog data. Every job and every chat sees the same
enabled set. This matches how the existing `skills` table works and is
the simplest thing that could possibly work.

Per-job overrides and per-user expert libraries are deliberate
follow-ups (see §6).

### 3.4 `model='inherit'` for every seeded expert

Upstream vendored files sometimes pin a specific model (`python-pro: opus`,
voltagent agents → `sonnet`). The user's Phase 0 policy decision was
**"Every expert uses model='inherit'"** — experts should cost the same
per-token as the parent orchestrator.

The seed helper enforces this by overriding any upstream `model` value
to `"inherit"` at parse time. The original upstream value is recoverable
from the vendored markdown file on disk if a future change decides to
respect upstream pins.

### 3.5 Foreign MCP tools are stripped at seed time

The `scientific-literature-researcher.md` upstream vendored file
references `mcp__bgpt__search_papers`, an MCP server that does not exist
in openscientist. Passing this through verbatim would cause the SDK to
register an expert with an unresolvable tool allowlist.

`expert_seed._parse_tools_field` drops any `mcp__<server>__<tool>` whose
prefix is not `mcp__openscientist-tools__`. Built-in tools (`Read`,
`Grep`, `WebFetch`, etc.) pass through unchanged. The behavior is
covered by `test_parse_strips_foreign_mcp_tools`.

### 3.6 NULL `model` in DB → `"inherit"` in SDK

The `Expert.model` column is nullable. The loader materializes
`NULL → "inherit"` so downstream code never has to think about `None`.
This matches the SDK's `AgentDefinition.model` which accepts the literal
string `"inherit"` explicitly.

### 3.7 Dynamic Expert Delegation section (no hardcoded roster)

The orchestrator's system prompt (`get_system_prompt()`) and the
JOB_CLAUDE.md template (`generate_job_claude_md()`) both need to teach
the agent which experts exist and when to delegate. Both now take an
optional `experts: dict[str, AgentDefinition] | None` parameter and
render the Expert Delegation section **from the actually-registered
experts**, not from a hardcoded list.

`_render_expert_delegation_section(experts)` in `prompts.py` is the
single source of truth for the section's shape: a static preamble, a
dynamically-generated bullet list of `slug · description` pairs, and
a static epilogue. When `experts` is `None` or empty, the function
returns an empty string and the section is **omitted entirely** from
the surrounding prompt. Silence is preferable to a stale roster:
telling the agent to invoke an expert that is not actually registered
would produce confusing runtime failures.

Load ordering matters: `_build_agent_executor` in `discovery.py`
**loads experts first**, then calls `get_system_prompt(experts=...)`,
then passes the same dict to `SDKAgentExecutor`. This guarantees the
prompt can only advertise slugs that are actually registered with the
SDK at session init. Disabling, renaming, or adding an expert via DB
UPDATE takes effect on the very next executor construction, with the
prompt tracking the change automatically.

### 3.8 Admin access via the project's dual-URL pattern

The `experts` table has admin-only RLS (`FOR ALL USING
(current_user_is_admin())`), so reading it requires a privileged
database connection. The project already has a canonical way to get
one: the **dual-URL pattern** with `ADMIN_DATABASE_URL` pointing at a
connection string that authenticates as `openscientist_admin` (a
role with `BYPASSRLS`), consumed by `get_admin_session()` in
`src/openscientist/database/session.py`.

Discovery and chat use `get_admin_session()` directly — not a custom
`SET ROLE` wrapper — so the expert loader path matches every other
privileged read in the codebase (`bootstrap.py`, `skill_scheduler.py`,
`api/auth.py`, etc.). An earlier draft of this work used a
thread-safe `SET ROLE openscientist_admin` helper built on top of
`AsyncSessionLocal(thread_safe=True)`; that worked in tests because
the test container's `openscientist` user is a superuser and can
freely switch roles, but in production it would require granting
`openscientist_admin` membership to the app user and still wouldn't
use the configured admin URL. Using `get_admin_session()` is
explicitly correct and testable.

`JobContainerRunner._build_container_environment` propagates
`ADMIN_DATABASE_URL` (via `settings.database.effective_admin_database_url`,
which falls back to `DATABASE_URL` when the admin URL is unset) into
the agent container env alongside `DATABASE_URL`, so the container
process can open admin sessions too. Without this propagation the
agent would silently fall back to the app user and RLS would return
zero experts.

### 3.9 Defense-in-depth against bad expert rows

The experts catalog is runtime-editable, which means a single bad
admin edit (e.g. `UPDATE experts SET model = 'gpt-4'`) must never be
able to take down every new executor. Two layers guard against this:

1. **Schema CHECK constraints** in the `add_expert_table` migration:
   - `ck_experts_model_valid`: `model IS NULL OR model IN ('sonnet',
     'opus', 'haiku', 'inherit')`
   - `ck_experts_tools_is_array`: `tools IS NULL OR jsonb_typeof(tools)
     = 'array'`

   These block bad state at write time, so the invalid row never
   reaches the loader in the first place.

2. **Loader resilience** in `load_enabled_experts`: per-row validation
   is wrapped in `try/except (ValueError, TypeError)`. A row that
   fails validation is **skipped with a logged warning**, and the
   loader continues processing the remaining rows. This is defense
   in depth for the scenario where bad state somehow slipped through
   the CHECK constraints (dropped constraint, migration bug, manual
   `pg_dump`/restore from an older schema).

The loader also explicitly validates that `row.tools` is a Python
`list` before calling `list(row.tools)` — a plain string slipping
through would otherwise be silently split char-by-char
(`list("abc") == ['a','b','c']`).

One ORM footgun surfaced while implementing the CHECK constraints:
by default SQLAlchemy serializes Python `None` for a JSONB column as
the JSON literal `null`, not SQL `NULL`. `jsonb_typeof('null'::jsonb)`
is `'null'`, not `'array'`, so the check would reject it.
`Expert.tools` is declared with `JSONB(none_as_null=True)` so
`None` → SQL `NULL`, which the check explicitly allows. The seed
migration and `run_seed` helper additionally build their insert
`values` dict conditionally — nullable fields with `None` are omitted
so SQLAlchemy sends nothing at all, and the column falls back to its
DDL default.

### 3.10 Frozen seed migration

Alembic migrations must be reproducible from any future checkout. The
first draft of the seed migration imported `all_seed_rows()` from
`openscientist.agent.expert_seed`, which in turn read the current
working-tree vendored files. That meant any later rename, removal, or
edit of those files would change what the *historical* revision does,
and could even break `alembic upgrade head` on a fresh checkout.

The migration now embeds the 8 seed rows as a module-level
`_SEED_ROWS: list[dict[str, Any]]` literal captured at the time it
was written. There is no import from `expert_seed` anywhere in the
file, and no file I/O at `upgrade()` time. A comment at the top of
the literal warns:

> DO NOT edit this literal — any change to the seeded catalog must
> ship as a new migration that UPDATEs or INSERTs rows after this one
> has run.

Runtime code (`expert_seed.all_seed_rows()`, `run_seed()`) continues
to read the live vendored files for non-migration callers — mostly
tests, and any future tooling that wants the current catalog. The
migration and the runtime helper were identical at the moment the
migration was written; after that they are allowed to drift, and the
frozen migration is immune.

### 3.11 Synthesized YAML frontmatter for Anthropic cookbook prompts

Three of the eight vendored files come from the Anthropic claude-cookbooks
research-agent reference (`research_lead_agent.md`, `research_subagent.md`,
`citations_agent.md`). These are raw system prompts upstream — they have
**no YAML frontmatter** because they were never meant to be Claude Code
subagent definitions.

We prepend a small YAML block (`name`, `description`, `model: inherit`,
`source: anthropic`) to each, with an explicit comment marker:

```yaml
---
name: research-lead
description: Use for high-level research strategy...
model: inherit
source: anthropic
# OpenScientist frontmatter added on top of upstream content (verbatim below).
# License: MIT — see source_url for upstream repository.
---
You are an expert research lead, focused on high-level research strategy...
```

The upstream content is preserved **byte-for-byte** below the marker.
The `source_url` in each vendored frontmatter points at the upstream
repository for attribution. This satisfies MIT requirements while
making the file parse correctly.

The other five files (wshobson + voltagent) already use the standard
`.claude/agents/*.md` frontmatter format and are vendored verbatim with
no edits.

### 3.12 `yaml.safe_load`, not a hand-rolled parser

The first version of the seed helper used a hand-rolled regex+split
parser for YAML frontmatter. The second review pass found that three
voltagent files have **double-quoted descriptions**:

```yaml
description: "Use this agent when you need comprehensive research..."
```

The hand-rolled parser was storing the value with the quotes intact,
which would degrade SDK auto-delegation (the routing heuristic matches
on plain text). Switched to `yaml.safe_load`, following the project
convention in `skill_ingestion.py:161`. Added `pyyaml>=6.0` as an
explicit dependency in `pyproject.toml` (it was already a transitive
dep, but better to declare what we use).

---

## 4. What was delivered

### 4.1 Database schema

New `experts` table created by Alembic migration `e31e63b8c2c0`
(`add_expert_table`). Columns:

| Column | Type | Nullable | Notes |
|---|---|---|---|
| `id` | UUID | no | UUIDv7 via `UUIDv7Mixin` |
| `slug` | varchar(255) | no | unique index |
| `name` | varchar(255) | no | display only |
| `description` | text | no | drives SDK auto-delegation |
| `prompt` | text | no | system prompt body |
| `category` | varchar(100) | no | research \| domain \| methodology \| code |
| `source` | varchar(100) | no | openscientist \| anthropic \| wshobson \| voltagent |
| `tools` | jsonb | yes | NULL = inherit parent's tools |
| `model` | varchar(50) | yes | NULL = inherit |
| `source_url` | text | yes | upstream attribution URL |
| `is_enabled` | bool | no | server default `true` |
| `created_at` / `updated_at` | timestamptz | no | from `UUIDv7Mixin` |

RLS: admin-only `FOR ALL USING (current_user_is_admin())`, matching the
`review_tokens` pattern. Granted to `openscientist_app` and
`openscientist_admin` roles.

### 4.2 Seeded experts

Eight vendored experts seeded by Alembic migration `e31e63b8c2c0`
(`add_expert_table`), idempotent via `INSERT ... ON CONFLICT (slug)
DO NOTHING`:

| Slug | Source | Category | Notes |
|---|---|---|---|
| `research-lead` | anthropic | research | OODA-loop research orchestrator |
| `research-subagent` | anthropic | research | bounded evidence gatherer |
| `citations-agent` | anthropic | methodology | post-hoc citation grounding |
| `data-scientist` | wshobson | methodology | stats / causal inference / power analysis |
| `python-pro` | wshobson | code | idiomatic Python tasks |
| `scientific-literature-researcher` | voltagent | research | structured literature retrieval |
| `data-researcher` | voltagent | research | dataset discovery + quality checks |
| `research-analyst` | voltagent | research | synthesis + structured reporting |

All eight have `model='inherit'` regardless of upstream pins. Attribution
fields populated for all eight.

### 4.3 Code changes

**New source files:**

- `src/openscientist/database/models/expert.py` — `Expert` SQLAlchemy model
- `src/openscientist/agent/expert_loader.py` —
  `load_enabled_experts(session) -> dict[str, AgentDefinition]`
- `src/openscientist/agent/expert_seed.py` — vendored-file parser,
  `SeedRow` dataclass, `all_seed_rows()`, `run_seed()`
- 1 Alembic migration (schema + seed in a single revision)
- 8 vendored `.md` files under
  `src/openscientist/agent/prompts/thirdparty/{anthropic,wshobson,voltagent}/`

**Modified source files:**

- `src/openscientist/agent/factory.py` — `get_agent_executor()` accepts
  and forwards `experts=`
- `src/openscientist/agent/sdk_executor.py` — `SDKAgentExecutor.__init__`
  accepts `experts=`, takes a defensive copy, passes to
  `ClaudeAgentOptions(agents=...)` in `_build_options()`
- `src/openscientist/orchestrator/discovery.py` — `_build_agent_executor`
  is now `async`, loads experts first, then builds a system prompt
  whose Expert Delegation section reflects exactly the loaded experts;
  `_write_skills_to_claude_dir` also threads experts into
  `_write_job_claude_md` so JOB_CLAUDE.md matches DB state
- `src/openscientist/job_chat.py` — new `_build_chat_executor` helper
  with the same pattern; `SDKAgentExecutor` import promoted to
  module-level so tests can patch it at the import site
- `src/openscientist/prompts.py` —
  `_render_expert_delegation_section(experts)` helper builds the
  delegation section from a runtime experts dict; `get_system_prompt`
  and `generate_job_claude_md` both take an optional `experts=` kwarg
  and omit the section when it is `None` or empty
- `src/openscientist/database/models/expert.py` — `tools` column uses
  `JSONB(none_as_null=True)` so Python `None` → SQL NULL (required
  by the new `ck_experts_tools_is_array` CHECK constraint)
- `src/openscientist/database/models/__init__.py` — exports `Expert`
- `pyproject.toml` — adds `pyyaml>=6.0` (was transitive)

### 4.4 Tests

**119 net new tests** across 10 new test files, plus new test sections
in 2 existing files (`test_prompts.py`, `test_hypothesis_feature.py`)
and minor updates to 3 more existing files for patch-target migration,
async-await adjustment, and container-env propagation.

| File | Tests | Purpose |
|---|---|---|
| `test_expert_model.py` | 8 | model CRUD, constraints, defaults |
| `test_expert_migration.py` | 7 | schema introspection + CHECK constraint enforcement |
| `test_expert_loader.py` | 15 | DB → `AgentDefinition` mapping, enabled filter, model normalization, deterministic ordering, resilience (bad row logged and skipped), admin-session integration |
| `test_sdk_executor_experts.py` | 6 | constructor accepts `experts` kwarg, defensive copy, no side effects on other options |
| `test_discovery_experts.py` | 3 | discovery wires loader → executor (mocked loader) |
| `test_job_chat_experts.py` | 4 | chat wires loader → executor (mocked loader), fail-open on expert-load error |
| `test_vendored_experts.py` | 38 | parametrized: file presence, frontmatter, slug contract |
| `test_expert_seed.py` | 7 | DB-level seed assertions, idempotency, attribution |
| `test_expert_seed_helper.py` | 18 | parser unit tests: quote-stripping, foreign-MCP-stripping, YAML list, inherit-model policy |
| `test_experts_end_to_end.py` | 3 | real DB → real loader → real executor with seeded experts |
| `test_prompts.py` (new section) | 6 | dynamic delegation rendering: absent when experts=None, reflects supplied dict, rejects stale slugs |
| `test_hypothesis_feature.py` (new section) | 4 | `generate_job_claude_md(experts=...)`: absent when None/empty, renders each slug, anti-drift |

**Test count:** 995 baseline → **1114 final, +119 tests, 0 regressions.**

---

## 5. Quality gates

| Check | Scope | Result |
|---|---|---|
| `ruff check` | `src/` + `tests/` | clean |
| `ruff format --check` | `src/` + `tests/` | clean |
| `mypy` (CI config) | `src/openscientist/` + `tests/` | clean |
| `mypy --strict` | all touched source files | clean |
| `pytest` | full suite | 1114 passed, 0 failed, 0 errors |
| Cold-import sanity | 9 touched modules | no circular imports |
| Alembic chain | end-to-end | linear, single head, downgrade tested |

The project's CI runs `mypy src/openscientist/ tests/` (not strict). All
new code passes both that and `mypy --strict` on the touched files.

---

## 6. Out of scope (deliberate gaps for follow-ups)

The following were considered and deliberately deferred:

- **Admin web UI** for CRUD on experts. The DB schema and RLS policy are
  ready; the NiceGUI page is not.
- **Per-user / per-job expert overrides.** A `job_expert` join table or
  similar would let owners enable a subset per job. Not needed yet.
- **Custom-written experts** filling the scientific gap left by the
  vendored set. Specifically:
  - `hypothesis-generator` — enumerate candidate causal mechanisms with
    falsifiability criteria
  - `methodology-critic` — red-team experimental design, confounders,
    p-hacking risk
  - `experimental-design-planner` — power analysis and next-step planning
  - `bioinformatics-expert`, `structural-biology-expert`, etc. — true
    domain experts
  - `reproducibility-auditor` — verify a result can be regenerated from
    code+data
  None of the surveyed third-party collections cover these.
- **Full port of Anthropic's `research_lead_agent.md` prompt structure**
  into the orchestrator system prompt. The user explicitly decided
  against this in Phase 0 — the existing knowledge-state-driven loop is
  proven and works.
- **Stripping Anthropic cookbook template placeholders.** The
  `research_lead_agent.md` and `research_subagent.md` files include
  `{{.CurrentDate}}` placeholders intended for Anthropic's internal
  templating system. They land in the DB verbatim and the agent will
  see them unresolved. Cosmetic; can be addressed later.
- **Consolidating `run_seed` and the migration insert loop.** Both sit
  in `expert_seed.py` and duplicate ~15 lines. Unifying requires either
  making `run_seed` sync (loses the AsyncSession-friendly API) or
  wrapping an async runner inside the migration. Not worth it for the
  small duplication.

---

## 7. What I believe was achieved

A working, end-to-end expert-subagent pipeline:

- A reviewer can run `uv run alembic upgrade head` against a fresh DB
  and get eight ready-to-use experts.
- A reviewer can start a discovery job and the agent will see the
  experts in its options dict at session init. Auto-delegation will
  route narrow tasks to them based on description.
- A reviewer can start a chat and the chat agent sees the same set.
- A reviewer can disable any expert via `UPDATE experts SET is_enabled
  = false WHERE slug = '...'` and the next executor build will exclude
  it. No code changes, no restart of the web server is required for
  the next job; the change takes effect on the next executor
  construction.

The choice to **vendor and adapt** rather than write from scratch
landed about 30% of the work for free, and provided structural
templates for the custom experts that will fill the remaining 70% in
follow-up PRs.

The single biggest risk going in was that auto-delegation would
mis-route across overlapping expert descriptions. The eight seeded
experts have deliberately distinct categories (research / methodology
/ code) and explicit "Use when..." trigger phrases. Whether routing
quality is acceptable in practice will only be known by running real
jobs and watching which experts get called. If it is poor, the
follow-up is to refine descriptions in the DB — no code changes
required.

The code is, I believe, **clean and maintainable**:

- Three review passes with TDD discipline throughout
- Single source of truth for the Expert Delegation section
- `_VALID_MODELS` derived from the `Literal` type via `get_args`
- Defensive copy of the experts dict in the executor constructor
- Frontmatter parsing via `yaml.safe_load`, matching project convention
- Foreign MCP tool filtering with explanatory comment
- Module/class/function docstrings explain *why*, not *what*
- Migration is idempotent; both `upgrade` and `downgrade` are tested

I am **not claiming** the routing quality is good — that requires
operational evidence. I am claiming the plumbing is sound and the
catalog is in a state where iterating on the prompts is a one-line
SQL update away.

---

## 8. References

- **Anthropic claude-code subagents docs**: <https://code.claude.com/docs/en/sub-agents>
- **claude-agent-sdk Python source**:
  `/.venv/lib/python3.12/site-packages/claude_agent_sdk/`
- **Vendored upstream sources**:
  - <https://github.com/anthropics/claude-cookbooks/tree/main/patterns/agents/prompts>
  - <https://github.com/wshobson/agents>
  - <https://github.com/VoltAgent/awesome-claude-code-subagents>
- **Auto-memory entries** (in `memory/`):
  - `MEMORY.md` — index
  - `sdk_subagents_loading.md` — empirical SDK behavior, why filesystem
    `.claude/agents/*.md` files do not work under our SDK options
