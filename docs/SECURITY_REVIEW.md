# Security Review

**Date:** 2026-03-10
**Scope:** Full codebase review of the OpenScientist web application

---

## Executive Summary

OpenScientist has a **solid security foundation**. User authentication relies on industry-standard OAuth (Google, GitHub), sessions are stored in the database with secure cookies, and every database table enforces row-level security so users can only see their own data. Agent jobs run inside isolated Docker containers with resource limits and privilege restrictions. Secrets are encrypted at rest, and API key verification uses constant-time comparison to prevent timing attacks.

The main areas for improvement are **operational security gaps** rather than architectural flaws: there is no audit logging for admin actions, most API endpoints lack rate limiting, the web server container runs as root, and there are no per-iteration timeouts or token budgets to prevent runaway agents. None of these are exploitable in a default deployment, but addressing them would significantly strengthen the overall security posture.

---

## What's Been Done Well

### Authentication and Sessions

- **OAuth 2.0 with major providers.** Users sign in through Google or GitHub using the Authlib library. No passwords are stored or managed by the application.
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
- **Code execution in separate containers.** When an agent needs to run Python or Rust code, it spawns a further-isolated executor container. Input data is mounted read-only.

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

### Critical

| Finding | Description | Recommended Next Step |
|---------|-------------|----------------------|
| **Web server runs as root** | The main application container does not set a non-root `USER` in its Dockerfile. If the web process is compromised, the attacker has root inside the container. | Add a non-root user to the Dockerfile and run the application as that user. |
| **Docker socket mounted read-write** | The web server and agent containers mount `/var/run/docker.sock` with read-write access. A compromise of either container could spawn arbitrary sibling containers on the host. | Evaluate whether the web server truly needs socket access, or if a Docker API proxy with restricted permissions could be used instead. |

### High

| Finding | Description | Recommended Next Step |
|---------|-------------|----------------------|
| **No rate limiting on most endpoints** | Only the health-check endpoint has rate limiting (10/minute via slowapi). Login, job creation, file upload, and token redemption endpoints have no throttling. | Extend rate limiting to all public-facing and authentication-related endpoints. |
| **No audit logging for admin actions** | Admin operations (user approvals, job reassignment, token creation/revocation) are logged to the application logger but not to a persistent, tamper-evident audit table. | Create an `audit_log` database table and record all admin actions with timestamps, actor, and details. |
| **No CSRF token validation** | While `samesite=lax` cookies provide partial protection, there is no explicit CSRF token for state-changing form submissions. | Add CSRF token generation and validation for all POST/PUT/DELETE operations. |
| **Credentials passed to agent containers via environment variables** | Provider API keys and the database URL (including password) are injected into agent containers as plain environment variables. If a container is compromised, all credentials are exposed. | Consider a secrets manager (e.g., HashiCorp Vault) or short-lived, scoped tokens instead of long-lived credentials. |
| **Base Docker image not pinned to digest** | Dockerfiles use `python:3.12-slim` without a SHA-256 digest pin, meaning a compromised or updated upstream image could silently change the build. | Pin to a specific image digest (e.g., `python:3.12-slim@sha256:abc...`). |

### Medium

| Finding | Description | Recommended Next Step |
|---------|-------------|----------------------|
| **No per-iteration timeout or token budget** | An individual agent iteration can run indefinitely and consume unlimited tokens. The only safety limit is the total iteration count (max 20). | Add a wall-clock timeout (e.g., 15 minutes) and a maximum token spend per iteration. |
| **No Content Security Policy (CSP) headers** | No CSP headers are set. While cookies are `httponly`, CSP would add defense-in-depth against cross-site scripting. | Configure CSP headers in the reverse proxy or application middleware. |
| **OpenAPI docs exposed without authentication** | `/api-docs`, `/api-redoc`, and `/openapi.json` are accessible to anyone, revealing the full API surface to potential attackers. | Require authentication for API documentation, or disable it in production. |
| **No automated dependency vulnerability scanning** | There is no evidence of Dependabot, Snyk, or similar tooling monitoring the 35+ direct dependencies for known CVEs. | Enable automated CVE scanning in CI (e.g., GitHub Dependabot, `pip-audit`, or Snyk). |
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
| Authentication (OAuth) | Strong | Industry-standard OAuth 2.0 with secure cookies |
| Session management | Strong | Database-backed, expiring, httponly/secure/samesite |
| Secrets at rest | Strong | Fernet encryption, HMAC-derived keys, hashed API keys |
| Database access control | Strong | Row-Level Security on all sensitive tables |
| Input validation | Strong | Pydantic schemas, file magic-number checks, parameterized SQL |
| Container isolation | Good | Per-job containers with resource limits and `no-new-privileges` |
| Job access control | Good | Ownership checks at API + database layer; explicit sharing |
| Review tokens | Good | Hashed, race-safe, revocable; but expiry should be mandatory |
| Rate limiting | Needs Work | Only one endpoint protected; all others are unthrottled |
| Audit logging | Needs Work | No persistent audit trail for admin or sensitive operations |
| CSRF protection | Needs Work | Partial (`samesite=lax`); no explicit token validation |
| Container privileges | Needs Work | Web server runs as root; Docker socket mounted read-write |
| Dependency scanning | Needs Work | No automated CVE monitoring |
| Execution guardrails | Needs Work | No per-iteration timeout or token budget |
