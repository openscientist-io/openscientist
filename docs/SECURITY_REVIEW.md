# Security Review

**Original review:** 2026-03-10
**Last updated:** 2026-08-26 — statuses re-verified against the current implementation
**Scope:** Full codebase review of the OpenScientist web application

A finding is marked *Resolved* here only where the remediation was verified in current code or configuration. Findings that could not be verified as fixed are preserved as open, unchanged.

---

## Executive Summary

OpenScientist has a **solid security foundation**. User authentication relies on industry-standard OAuth (Google, GitHub, ORCID), sessions are stored in the database with secure cookies, and every sensitive database table enforces row-level security so users can only see their own data. Agent jobs run inside isolated Docker containers with resource limits and privilege restrictions. Secrets are encrypted at rest, and API key verification uses constant-time comparison to prevent timing attacks.

Four findings from the original review have since been remediated and verified: the raw Docker socket mount was replaced with a restricted socket proxy, all application images now run as non-root users, rate limiting was extended from a single endpoint to authentication and state-changing endpoints, and automated dependency vulnerability scanning is now enforced in CI.

Most remaining gaps are **operational**: there is no audit logging for admin actions, no explicit CSRF token validation, no per-iteration timeout or token budget to bound a runaway agent, and base images are not pinned to digests.

One architectural gap persists. The socket proxy must permit container creation for jobs to run, and it authorises by API path and method rather than by request content, so a compromised application container can still create a sibling container with arbitrary bind mounts and reach the host. The proxy is a substantial reduction in reachable API surface, not a container-escape boundary; it is tracked as an open High finding below.

---

## What's Been Done Well

### Authentication and Sessions

- **OAuth 2.0 with major providers.** Users sign in through Google, GitHub, or ORCID using the Authlib library (a mock provider is available for development only). No passwords are stored or managed by the application.
- **Secure session cookies.** Session cookies are marked `httponly` (not readable by JavaScript), `secure` (only sent over HTTPS), and `samesite=lax` (mitigates cross-site request forgery). Sessions have a configurable expiry (default 30 days).
- **Database-backed sessions.** Sessions live in PostgreSQL with UUID primary keys and expiry timestamps, not in browser-side storage. Logging out deletes the database record.
- **User approval workflow.** New users must be explicitly approved by an admin before they can run jobs, preventing open access.

### Data Protection

- **Row-Level Security (RLS).** PostgreSQL RLS policies are enabled on all sensitive tables (users, sessions, jobs, API keys, OAuth accounts). Even if application code has a bug, the database itself prevents users from seeing each other's data.
- **Encrypted OAuth tokens.** Access and refresh tokens from OAuth providers are encrypted with Fernet (AES-128-CBC) before being stored in the database.
- **Master secret derivation.** A single `OPENSCIENTIST_SECRET_KEY` is used to derive separate keys for different purposes (session storage, token encryption) via HMAC-SHA256, following key derivation best practices.
- **API keys hashed with SHA-256.** API key secrets are hashed before storage. The full secret is shown only once at creation time and is never retrievable afterwards.
- **Constant-time secret comparison.** API key verification uses `hmac.compare_digest()` to prevent timing side-channel attacks.

### Container Isolation

- **One container per job.** Each agent job runs in its own ephemeral Docker container, preventing jobs from interfering with each other.
- **Resource limits enforced.** Agent containers have configurable CPU and memory caps (default: 2 CPUs, 8 GB RAM).
- **Privilege escalation blocked.** Containers run with `no-new-privileges` and the agent process runs as a non-root user (UID 1001).
- **Non-root across all images.** The web application (`USER openscientist`, UID 1001), agent (`USER agent`, UID 1001), and executor (`USER executor`) images all drop to a non-root user.
- **Code execution in separate containers.** When an agent needs to run Python or Rust code, it spawns a further-isolated executor container with a read-only root filesystem. Input data is mounted read-only.
- **No raw Docker socket in application containers.** A `docker-socket-proxy` sidecar is the only container that mounts `/var/run/docker.sock`; everything else reaches the Docker API through it over `DOCKER_HOST`. `tests/test_docker_compose_security.py` asserts these properties, so the boundary is regression-tested rather than only documented.

### Input Validation

- **Pydantic models for API input.** All API request bodies are validated through Pydantic V2 schemas with type checking and length constraints (e.g., job titles: 1--255 characters, max iterations: 1--20).
- **File upload safety checks.** Uploaded files are validated by size, extension, and magic-number detection (using `python-magic`). Executable files (ELF, Mach-O, DOS) are blocked. Filenames are sanitized to prevent path traversal.
- **Parameterized SQL queries.** All database access uses SQLAlchemy ORM, which generates parameterized queries. No raw SQL string interpolation was found.
- **Vulnerability scanner blocking.** A middleware layer blocks common scanner paths (`.git/`, `wp-admin/`, etc.) to reduce noise and exposure.

### Access Control

- **Job ownership enforced at every layer.** API endpoints verify that the requesting user owns (or has been explicitly shared on) a job. RLS provides a database-level safety net.
- **Explicit sharing model.** Jobs can be shared with specific users at "view" or "edit" permission levels through a dedicated `job_shares` table.
- **Admin access requires both authentication and admin status.** Admin pages are protected by stacked `@require_auth` and `@require_admin` decorators. Admins cannot remove their own approval.

### Review Tokens

- **Tokens are hashed before storage.** Review tokens are generated with `secrets.token_urlsafe(32)` (128 bits of entropy) and stored as SHA-256 hashes. The database never holds the plaintext.
- **Race-condition safe redemption.** Token lookup uses `SELECT ... FOR UPDATE` to prevent double-redemption.
- **Referrer leakage prevention.** Redemption responses set `Referrer-Policy: no-referrer` so the token doesn't leak in HTTP headers.
- **Revocation support.** Tokens can be deactivated by admins, immediately blocking further use.

### Secrets Management

- **No secrets in code.** All credentials come from environment variables or TOML configuration, never from hardcoded values.
- **Credentials not logged.** A search for credential patterns in logging output found no evidence of API keys, tokens, or passwords being written to logs.

---

## What's Missing or Could Be Improved

### Resolved Since the Original Review

| Finding | Original severity | Verification |
|---------|-------------------|--------------|
| **Web server runs as root** | Critical | **Resolved.** `Dockerfile` creates the `openscientist` user/group (UID/GID 1001) and sets `USER openscientist` before `CMD`. `Dockerfile.agent` sets `USER agent` (UID 1001) and `Dockerfile.executor` sets `USER executor`. No application image now runs as root. |
| **Docker socket mounted read-write** | Critical | **Resolved.** The raw socket mount is removed. A `docker-socket-proxy` service is the only container with socket access (mounted read-only, no published ports); the web app and agent containers reach it over `DOCKER_HOST=tcp://docker-socket-proxy:2375`, restricted to the container/image verbs the job lifecycle needs (create/start/stop/wait/list/inspect/logs/remove, plus image inspect/pull). `EXEC`, `INFO`, `NETWORKS`, `VOLUMES`, `BUILD`, `SWARM`, and `SECRETS` are denied. Asserted by `tests/test_docker_compose_security.py`. **Residual risk remains** — see the open finding below. |
| **No rate limiting on most endpoints** | High | **Largely resolved.** slowapi limits are now applied via `src/openscientist/api/rate_limits.py`: `AUTH_RATE_LIMIT` (10/minute) across all seven auth routes in `auth/fastapi_routes.py`, `MUTATING_RATE_LIMIT` (30/minute) on state-changing job, key, share, and skill endpoints plus the webapp share routes, and `HEALTH_RATE_LIMIT` (10/minute) on `/health`. Read-only endpoints remain unthrottled — see the open finding below. |
| **No automated dependency vulnerability scanning** | Medium | **Resolved.** CI runs `pip-audit --local --desc` on every push and pull request and `actions/dependency-review-action@v4` with `fail-on-severity: high` on pull requests. `.github/dependabot.yml` opens weekly update PRs for pip, Docker, and GitHub Actions. gitleaks secret scanning runs in CI and as a pre-commit hook. |

### High

| Finding | Description | Recommended Next Step |
|---------|-------------|----------------------|
| **Container creation via the socket proxy is still a host-escape vector** | Residual risk after the socket-proxy remediation above. The proxy must permit `POST /containers/create` for the job lifecycle to work (`CONTAINERS: 1`, `POST: 1`), and it authorises by API path and HTTP method — it does not inspect request bodies. A compromised web or agent container can therefore create a sibling container with arbitrary bind mounts (for example `/:/host`) or privileged options and read or modify the host filesystem. The proxy substantially reduces the reachable API surface compared with a raw socket mount, but it is not a container-escape boundary. | Filter creation requests rather than only gating verbs — for example an authorising broker that validates image, mounts, and security options against an allowlist, or moving job execution to a runtime that does not require Docker API access from the application. |
| **No audit logging for admin actions** | Admin operations (user approvals, job reassignment, token creation/revocation) are logged to the application logger but not to a persistent, tamper-evident audit table. | Create an `audit_log` database table and record all admin actions with timestamps, actor, and details. |
| **No CSRF token validation** | While `samesite=lax` cookies provide partial protection, there is no explicit CSRF token for state-changing form submissions. | Add CSRF token generation and validation for all POST/PUT/DELETE operations. |
| **Credentials passed to agent containers via environment variables** | Provider API keys and the database URL (including password) are injected into agent containers as plain environment variables. If a container is compromised, all credentials are exposed. | Consider a secrets manager (e.g., HashiCorp Vault) or short-lived, scoped tokens instead of long-lived credentials. |
| **Base Docker image not pinned to digest** | Dockerfiles use `python:3.12-slim` without a SHA-256 digest pin, meaning a compromised or updated upstream image could silently change the build. | Pin to a specific image digest (e.g., `python:3.12-slim@sha256:abc...`). |

### Medium

| Finding | Description | Recommended Next Step |
|---------|-------------|----------------------|
| **No per-iteration timeout or token budget** | An individual agent iteration can run indefinitely and consume unlimited tokens. The only safety limit is the total iteration count (max 20). | Add a wall-clock timeout (e.g., 15 minutes) and a maximum token spend per iteration. |
| **No Content Security Policy (CSP) headers** | No CSP headers are set. While cookies are `httponly`, CSP would add defense-in-depth against cross-site scripting. | Configure CSP headers in the reverse proxy or application middleware. |
| **OpenAPI docs exposed without authentication** | `/api-docs`, `/api-redoc`, and `/openapi.json` are registered on the host app with no auth dependency (`web_app.py`), so the full `/api/` surface is readable by anyone. Still open. | Require authentication for API documentation, or disable it in production. |
| **Read-only endpoints remain unthrottled** | Rate limiting now covers authentication and state-changing endpoints, but `GET` endpoints (job listing, job detail, artifact retrieval) have no limit and can still be used to hammer the service. | Extend `slowapi` coverage to read endpoints, or apply a global default limit. |
| **No secret key rotation mechanism** | Changing `OPENSCIENTIST_SECRET_KEY` invalidates all sessions and encrypted data. There is no graceful rotation workflow. | Implement key rotation support that re-encrypts data with the new key while still accepting the old key during a transition window. |
| **Job shares don't expire** | Once a job is shared with another user, access is permanent unless manually revoked. | Add optional expiry to job shares and a user-facing revocation interface. |
| **Review tokens don't require expiry** | Token expiry is optional. A never-expiring token that leaks could be used indefinitely. | Make expiry mandatory with a reasonable default (e.g., 7 days, maximum 1 year). |
| **No seccomp or AppArmor profiles** | Agent and executor containers rely only on `no-new-privileges`. No system-call filtering is applied. | Define seccomp profiles to restrict unnecessary syscalls in agent and executor containers. |
| **World-writable job directories on the host** | Job directories are set to `chmod 0o777` to handle UID mismatches between containers. On a shared host, any local user could read or modify job data. | Use consistent UIDs across containers to allow more restrictive permissions (e.g., `0o750`). |

### Low

| Finding | Description | Recommended Next Step |
|---------|-------------|----------------------|
| **Session tokens are UUIDs** | Session tokens are standard UUID4 values. While 128-bit random, cryptographic tokens (`secrets.token_urlsafe`) would be marginally harder to predict. | Consider switching to `secrets.token_urlsafe(32)` for session identifiers. |
| **Email validation is minimal** | Email addresses are validated with a simple regex (`^[^@\s]+@[^@\s]+$`). Invalid formats like `a@b` pass validation. | Use a proper email validation library (e.g., `email-validator`). |
| **Agent code imports are permissive** | The allowed-imports list for Python code execution includes `requests` (network access) and `os` (environment access). | Review and tighten the import allowlist; consider removing `os` or restricting it to `os.path`. |
| **Research questions logged as-is** | Job titles and research questions appear in application logs without PII scrubbing. | Evaluate whether research context in logs is acceptable for your compliance requirements. |
| **Log files stored as plaintext** | Iteration logs and agent transcripts are written as unencrypted JSON and text files in the job directory. | Consider encrypting log files at rest if they may contain sensitive research content. |
| **Admin pages don't require reauthentication** | An attacker with access to an open admin browser session can perform admin actions without entering a password. | Require reauthentication (or a session timeout) before sensitive admin operations. |
| **No SBOM (Software Bill of Materials)** | No CycloneDX or SPDX manifest is generated, making it harder to respond quickly to supply-chain advisories. | Add SBOM generation to the CI pipeline. |

---

## Summary Table

| Area | Status | Notes |
|------|--------|-------|
| Authentication (OAuth) | Strong | Industry-standard OAuth 2.0 (Google, GitHub, ORCID) with secure cookies |
| Session management | Strong | Database-backed, expiring, httponly/secure/samesite |
| Secrets at rest | Strong | Fernet encryption, HMAC-derived keys, hashed API keys |
| Database access control | Strong | Row-Level Security on all sensitive tables |
| Input validation | Strong | Pydantic schemas, file magic-number checks, parameterized SQL |
| Container isolation | Good | Per-job containers, read-only executor FS, resource limits, `no-new-privileges`, restricted socket proxy |
| Container privileges | Good | All images run non-root and no raw Docker socket is mounted; but permitted container creation remains a host-escape vector (see open finding) |
| Dependency scanning | Strong | `pip-audit` + dependency review in CI, weekly Dependabot, gitleaks secret scanning |
| Job access control | Good | Ownership checks at API + database layer; explicit sharing |
| Review tokens | Good | Hashed, race-safe, revocable; but expiry should be mandatory |
| Rate limiting | Good | Auth (10/min) and mutating (30/min) endpoints throttled; read endpoints still unthrottled |
| Audit logging | Needs Work | No persistent audit trail for admin or sensitive operations |
| CSRF protection | Needs Work | Partial (`samesite=lax`); no explicit token validation |
| Image provenance | Needs Work | Base images not pinned to SHA-256 digests; no SBOM |
| Execution guardrails | Needs Work | No per-iteration timeout or token budget |
| API documentation exposure | Needs Work | `/api-docs`, `/api-redoc`, `/openapi.json` unauthenticated |
