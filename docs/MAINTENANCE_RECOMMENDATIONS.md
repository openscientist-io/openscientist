# Maintenance Recommendations

> Source: internal documentation, converted from the shared drive. Keep this file as the canonical version.
**Date:** 2026-08-11
**Scope:** Ongoing practices for keeping OpenScientist's existing systems healthy over time, in line with CLAUDE.md and CONTRIBUTING.md.

## Dependencies
uv.lock pins exact versions and .github/dependabot.yml already opens update PRs. The main discipline is reviewing and merging those PRs on a regular cadence rather than letting them queue up. The longer a dependency bump sits open, the more other changes land on top of it and the harder the eventual merge/triage gets. Revisit the Python version floor (requires-python) periodically too, as 3.13+ stabilizes across the dependency set (numpy, pandas, scanpy in particular tend to lag on new Python support).

## Database migrations
Each new Alembic migration is a permanent link in a chain every test run and deployment depends on (test_alembic_migrations.py, conftest.py). Keep migrations small and reversible where practical, and when adding tables that hold user data, extend the existing RLS policy pattern (docker/postgres/init.sql) at the same time rather than as a follow-up. RLS gaps are easy to miss once a table already has data in it.

## Docker base images
Dockerfile.base and Dockerfile.executor only get rebuilt in CI when Docker-relevant paths change (per docs/QA_FRAMEWORK.md's CI gate description). That means OS-level security patches in the base image don't get picked up unless something else in the repo also changes. Consider periodic (e.g. monthly) rebuilds independent of code changes, so base-image CVEs don't sit unpatched between unrelated Dockerfile edits.

## LLM provider integrations
src/openscientist/providers/ wraps several backends (Anthropic, Vertex, Bedrock, CBORG, Foundry, Azure OpenAI, OpenAI, Ollama). Provider SDKs and model IDs deprecate on their own schedules, independent of this repo's release cycle. Periodically check each provider's changelog/deprecation notices, particularly for default model IDs (OPENSCIENTIST_MODEL) that may quietly stop being served.

## Secrets and key rotation
OPENSCIENTIST_SECRET_KEY derives all session and token-encryption keys (per docs/SECURITY_REVIEW.md). There's no rotation mechanism today, which is fine as a starting posture but worth revisiting as the user base grows. Rotating it invalidates all sessions and re-encrypts stored OAuth tokens, so it's the kind of change better designed ahead of an incident than during one.

## Job and container lifecycle
Containers are created per job (job_container/runner.py) and jobs persist as both database rows and (for legacy jobs) filesystem artifacts. Periodically confirm orphaned containers and stale job directories are actually being cleaned up in practice, not just in the shutdown-path tests (test_job_manager_shutdown_e2e.py).  A long-running deployment is the case if those tests can't fully simulate.

## Documentation currency
Some of the documentation is based on reviews done at a specific point in time, so it can become outdated as the project changes. The security review should be updated on a regular schedule, for example once a year, and also whenever there are major changes to authentication or container isolation. This helps keep the documentation accurate and ensures that security risks are reviewed regularly rather than only when someone notices that the information is outdated.
