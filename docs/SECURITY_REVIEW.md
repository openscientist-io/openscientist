# Security Review

**Date:** 2026-03-10
**Scope:** Full codebase review of the OpenScientist web application

---

## Executive Summary

OpenScientist has a **solid security foundation**. User authentication relies on industry-standard OAuth (Google, GitHub), sessions are stored in the database with secure cookies, and every database table enforces row-level security so users can only see their own data. Agent jobs run inside isolated Docker containers with resource limits and privilege restrictions. Secrets are encrypted at rest, and API key verification uses constant-time comparison to prevent timing attacks.

The main areas for improvement are **operational security gaps** rather than architectural flaws: there is no audit logging for admin actions, most API endpoints lack rate limiting, and there are no per-iteration timeouts or token budgets to prevent runaway agents. None of these are exploitable in a default deployment, but addressing them would significantly strengthen the overall security posture.

---

## What's Been Done Well

### Authentication and Sessions

- **OAuth 2.0 with major providers.** Users sign in through Google or GitHub using the Authlib library. No passwords are stored or managed by the application.
- **Verified email required for new identities.** A new OAuth identity is linked to an existing user, or creates a user, only when the provider reports the email address as verified; sign-in with an unverified address is refused.
- **Secure session cookies.** The login cookie is marked `httponly` (not readable by JavaScript), `secure` (only sent over HTTPS when the app URL is https), and `samesite=lax` (mitigates cross-site request forgery). The `Secure` flag follows the scheme of the deployment's base URL. The setup wizard writes `APP_URL` from the public base URL it asks for, and `APP_URL` falls back to `OPENSCIENTIST_BASE_URL` when unset, so both settings keep the real scheme. Sessions have a configurable expiry (default 30 days).
- **Socket.io restricted to the app's origin.** The socket.io server accepts handshakes only from the origin of the configured base URL, not from arbitrary origins.
- **Secure session cookies.** The login cookie is marked `httponly` (not readable by JavaScript), `secure` (only sent over HTTPS when the app URL is https), and `samesite=lax` (mitigates cross-site request forgery). Sessions have a configurable expiry (default 30 days).
- **Database-backed sessions.** Login cookies contain `secrets.token_urlsafe(32)` secrets with 256 bits of entropy. PostgreSQL stores only their `SHA-256` hashes, separate from the row identifiers. Page builds and `/web/*` routes look up the cookie hash and reject expired or deleted sessions. Logout deletes the row and clears both the login cookie and NiceGUI's browser-storage cookie. The session credential migration invalidates all sessions on upgrade and downgrade.
- **User approval workflow.** New users must be explicitly approved by an admin before they can run jobs, preventing open access.

### Data Protection

- **Row-Level Security (RLS).** PostgreSQL RLS policies are enabled on all sensitive tables (users, sessions, jobs, API keys, OAuth accounts). Even if application code has a bug, the database itself prevents users from seeing each other's data.
- **Encrypted OAuth tokens.** Access and refresh tokens from OAuth providers are encrypted with Fernet (AES-128-CBC) before being stored in the database.
- **Master secret derivation.** A single `OPENSCIENTIST_SECRET_KEY` is used to derive separate keys for different purposes (session storage, token encryption) via HMAC-SHA256, following key derivation best practices.
- **Weak master secrets are refused at startup.** Settings construction raises when `OPENSCIENTIST_SECRET_KEY` is missing, the `.env.example` placeholder, or shorter than 64 characters (32 bytes of entropy), and the web refuses to start with an error naming the setting and how to generate a key.
- **Error pages carry no exception text.** The 500 page for unhandled page exceptions renders a generic error, and the full traceback goes to the server log.
- **API keys hashed with SHA-256.** API key secrets are hashed before storage. The full secret is shown only once at creation time and is never retrievable afterwards.
- **Constant-time secret comparison.** API key verification uses `hmac.compare_digest()` to prevent timing side-channel attacks.

### Container Isolation

- **One container per job.** Each agent job runs in its own ephemeral Docker container, preventing jobs from interfering with each other.
- **Container logs replace existing filesystem entries.** Teardown saves `agent-container.log` through a staged write relative to a pinned job-directory descriptor, replacing an existing symlink or FIFO at that name without opening it. A log-write failure does not prevent container removal or run credential revocation.
- **Resource limits enforced.** Agent and executor containers have configurable CPU, memory and process caps. The process cap includes threads and defaults to 4096 per container, bounded by the launcher's `AGENT_MAX_PIDS` and `EXECUTOR_MAX_PIDS` settings. Each container has a `nofile` limit of 65536. The execution broker refuses calls above `OPENSCIENTIST_EXECUTOR_MAX_CONCURRENT_PER_JOB` with HTTP 429, with a default of two simultaneous executions per job. A disconnected caller keeps its slot until its execution finishes.
- **Privilege escalation blocked.** Containers run with `no-new-privileges` and the agent process runs as a non-root user (UID 1001).
- **Job files owned by the container users.** Job directories are setgid and owned by the agent's fixed UID and shared jobs group, so every file created in them inherits the group. The agent and executor containers run under umask 0007, so files and directories the job processes create are group-only (0660/02770) with no world bits, and operator logins copied into a run's home are private to the agent, so no other local account can read or modify a job's data.
- **Code execution in separate containers.** When an agent needs to run Python or Rust code, it spawns a further-isolated executor container. Input data is mounted read-only. The execution broker clamps each request's timeout to the admin settings' executor timeout (at least 300 s for Rust, which compiles before it runs), refusing zero, negative, non-numeric and non-finite values, and caps the size of the result string fields and plot list it returns.
- **LLM provider credentials stay out of the agent container.** The container receives a per-run placeholder instead of the real LLM provider credential. Provider credential environment variables are removed unless the proxy override supplies that placeholder, including inactive credentials when several authentication methods are configured. A web-side LLM proxy verifies the placeholder, swaps in the real credential, and streams the response back. For Vertex AI the web signs each request with its own GCP service-account credential, and for AWS Bedrock it signs each request with SigV4 using its own AWS credential, so the container holds no GCP credential file, no Google token, and no AWS key. For OpenAI's ChatGPT login the web holds the codex `auth.json`, refreshing its access token single-flight and writing the refreshed file back, and signs the Codex Responses calls upstream with it. The proxy terminates the Codex websocket and the REST fallback, and the container's Codex runs API-key mode against the proxy with the placeholder. For the omp harness the web holds the operator's login vault (`OMP_AUTH_HOST_PATH`) and signs the proxied requests for each vault provider (anthropic, github-copilot, openai-codex) with the vault's current access token, refreshing it single-flight and writing the refreshed row back so the host's own omp stays current; the job container holds no vault. A wrong or ended placeholder is refused without any upstream call.
- **The LLM proxy restricts upstream access.** Each provider has an allowlist of harness inference, token-counting, connectivity and model-listing endpoints by method and path. Dot segments are refused, and encoded separators are accepted only inside validated Bedrock model ARNs. Vertex requests must name the configured project, and caller-supplied `x-goog-*` billing and routing headers are discarded.
- Vertex token counting at `/models/count-tokens:rawPredict` passes through without budget admission or cost rows.
- **LLM request buffering is bounded.** HTTP bodies and websocket messages are limited to 128 MiB. Oversized declared lengths are rejected before reading, streamed bodies stop at the limit, and the websocket transport queues at most one message.
- **Costs are metered at the proxy, and the web is the only writer of cost rows.** Every forwarded model call is teed: the response bytes pass through unchanged while the proxy reads the token buckets out of the wire (JSON, SSE, the Bedrock binary event-stream, and the Codex websocket's `response.completed`), and at response end it writes one `cost_records` row through the web's admin login, attributed to the job, the iteration and the phase (discovery, report, chat) the call belongs to. The agent login has no INSERT on `cost_records`, and CHECK constraints refuse negative token or cost values, so a compromised container can neither bill itself nor hide spend. The same cutover moves the slot end time, which the container could otherwise stretch to weigh its owner down in the scheduler's fair-share sums. The agent login no longer has UPDATE on `jobs.ended_at`, the web stamps the time on the ending it writes, and the worker's poll backfills the ending the container wrote.
- **The proxy enforces spend limits before model calls.** It checks the job cap and project total and 24-hour limits against the cost ledger and cached provider billing. Exhausted runs receive an error before an HTTP model call or websocket handshake reaches the provider, and each websocket response creation is checked again. Calls already in flight can finish and add their cost to the ledger.
- **Expenditure survives job deletion.** Cost rows retain their provider and recorded owner independently of the job, so deleting jobs cannot reset project or 24-hour budgets. The admin billing panel includes lifetime per-user totals, and account deletion retains the expenditure without user attribution. The application role cannot update or delete cost rows, and the migration refuses a downgrade that would discard expenditure from deleted jobs.
- **Web server runs as a non-root user.** The web image runs as its own user (UID 1002) in the jobs group (GID 1001), so a compromised web process has no root inside its container.
- **Only the launcher holds the Docker socket.** The web and migrate services have no socket mount and no docker group. Every container operation the web makes — agent launches, chat turns, executor runs, stops, cleanup and the dashboard — goes through the launcher service over a unix socket that the launcher guards with a peer-credential check. The launcher builds every container from one of two server-side templates, and answers a uniform 404 for any container it did not create, so a compromised web cannot reach the Docker API, alter a container's mounts, capabilities, image or network, or touch a foreign container.

### Chat Admission

Each web process admits one chat turn per job and at most `OPENSCIENTIST_MAX_CONCURRENT_CHATS` turns across jobs. The global limit defaults to four. Each chat container uses the `OPENSCIENTIST_AGENT_MEMORY` limit, so operators must size concurrency to their host's available memory. Admission covers request preparation and container execution. A disconnected caller retains its slot until the container worker finishes.

| Event | No admitted turn | Preparing request | Running container | Caller cancelled during execution |
|---|---|---|---|---|
| Send for this job | Admit if global capacity permits | Refuse duplicate | Refuse duplicate | Refuse duplicate |
| Send for another job | Admit if global capacity permits | Admit if global capacity permits | Admit if global capacity permits | Admit if global capacity permits |
| Request prepared | Not applicable | Start container worker | Not applicable | Not applicable |
| Preparation fails or caller cancels preparation | Not applicable | Remove request files and release slot | Not applicable | Not applicable |
| Caller cancels execution | Not applicable | Not applicable | Keep worker and slot | Keep worker and slot |
| Launch fails, container errors, times out, or returns a reply | Not applicable | Not applicable | Remove turn files and release slot | Remove turn files and release slot |

### Input Validation

- **Pydantic models for API input.** All API request bodies are validated through Pydantic V2 schemas with type checking and length constraints (e.g., job titles: 1--255 characters, max iterations: 1--20).
- **File upload safety checks.** API uploads stream to temporary files in 1 MiB chunks and are refused with HTTP 413 when a file exceeds the `MAX_FILE_SIZE_MB` admin limit, when the request total does, or when a request holds more than 100 files. The multipart parser spools each part to disk past 1 MiB, so a part is never held in process memory whole. Accepted uploads are then validated by extension and magic-number detection of their leading bytes (using `python-magic`), and executable files (ELF, Mach-O, DOS) are blocked. Web uploads honour the same per-file limit and stream to disk in 1 MiB chunks. Filenames are sanitized to prevent path traversal.
- **Bounded image previews.** Images over 20 million pixels and images that trigger Pillow's oversized-image warning receive no preview. The pixel check runs before decoding, and job creation keeps the uploaded original.
- **Parameterized SQL queries.** All database access uses SQLAlchemy ORM, which generates parameterized queries. No raw SQL string interpolation was found.
- **Job page renders no agent-written HTML.** The research question shows as the scientist wrote it, and the page's only HTML built from job data is the attachment links, whose URLs and names the page escapes itself. A reader who sees the job only because it is public gets no attachment URLs, so the page cannot show the owner's uploads to them.
- **Report diagrams have bounded work.** Each report renders at most eight `DOT` blocks within a shared two-second Graphviz budget. Remaining blocks display escaped source. A process-local cache retains the 16 most recently used source hashes and their sanitized `SVG` or permanent failure results. Shortened timeouts and exhausted budgets remain retryable. Graphviz output is limited to two million characters per diagram. Cached diagrams remain available while Graphviz is busy, and uncached diagrams fall back to source without waiting.
- **Vulnerability scanner blocking.** A middleware layer blocks common scanner paths (`.git/`, `wp-admin/`, etc.) to reduce noise and exposure.

### Security Headers

- **Strict Content-Security-Policy on every response.** The web application emits a CSP that confines scripts and styles to the app's own origin, limits network connections to the same origin (`connect-src 'self'`), and disallows plugins, foreign `<base>` tags, and framing (`object-src 'none'`, `base-uri 'self'`, `frame-ancestors 'none'`).
- **Clickjacking and MIME-sniffing protection.** Responses also carry `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, a `Referrer-Policy`, and a restrictive `Permissions-Policy` that disables camera, microphone, geolocation, and payment.
- **HSTS over HTTPS.** When the configured app URL is served over HTTPS, a `Strict-Transport-Security` header is emitted.
- **Sandboxed report and artifact frames.** Agent-generated documents use `default-src 'none'`, same-origin or embedded images, inline styles, and no forms or nested frames. Reports also allow embedded fonts and a hash-authorized link handler. Clicking a report link opens a separate tab with its destination's permissions.

### Access Control

- **Job ownership enforced at every layer.** API endpoints verify that the requesting user owns (or has been explicitly shared on) a job. RLS provides a database-level safety net.
- **Grants match the code's usage.** The app role's `UPDATE` on `jobs` is limited to the run columns the web writes, the status-move columns `started_at`, `ended_at`, `error_message`, `stop_note`, `cancellation_reason` and `queue_reason`, the provenance `version_info` stamp, the knowledge-state iteration columns, the owner's `is_public` toggle and the ORM's `updated_at` stamp, so the owner's definition fields such as `owner_id`, the research question and the configuration columns stay out of reach of a share or an injection running as the web role. The junction tables `finding_hypotheses` and `finding_literature` are read by any share of either parent job, and only the owner or an `edit` share may write them. The SECURITY DEFINER helpers `check_job_owner`, `is_admin` and `current_user_is_admin` are revoked from PUBLIC, with `openscientist_app` the only role granted the two its policies call. The `openscientist_app` and `openscientist_agent` roles hold no TRUNCATE, on the existing tables or on any table created after the grant.
- **Explicit sharing model.** Jobs can be shared with specific users at "view" or "edit" permission levels through a dedicated `job_shares` table. Share targets are resolved by exact, case-insensitive email match only, and that lookup requires an administrator-approved caller (administrators included, via the same `user_can_start_jobs` gate job creation uses). No user directory or partial-string search is exposed.
- **Share dialog is owner-gated.** The web share dialog loads a job's share list (recipient names, emails and permission levels) only after an ownership check against the signed-in caller, and is built only for the job's owner, on the jobs list and the job page.
- **Public jobs are opt-in and scope-limited.** The owner sets `jobs.is_public` and, from then on, any signed-in user with the job's link or ID can read the job and its analysis rows (hypotheses, findings, literature, analysis log, iteration summaries, feedback, plots, and the two junction tables). A `jobs_select_public` `SELECT`-only RLS policy scoped to `openscientist_app` is the only thing that admits them, so the agent login (which runs as its own role) still reaches only its own job. Uploaded input files (`job_data_files`, the job directory's `data/`) and their viewing copies in `previews/`, chat messages, cost records and the share list stay owner/share-only. The artifact and report routes withhold `data/` and `previews/` from a public-only reader while keeping generated figures available. Chat turn files (`.chat_request_*.json` and `.chat_response_*.json`), which hold the owner's in-flight conversation, are excluded from the artifacts zip and the single-file route for every viewer, including the owner. A job left ownerless when its user is deleted is readable only by a signed-in admin, whose RLS policy test calls `current_user_is_admin()`, so the admin page can still list and reassign it. Public jobs stay off other users' job lists, which filter by owner. A database trigger keeps `is_public` owner-only even though the app role may `UPDATE` the run columns of a shared job. Sessions with no signed-in user (admin and agent) are unaffected.
- **Public reports are rendered on the web.** Public-only readers receive live `HTML` and uncached `PDF` reports with image embeds from `data/` and `previews/` excluded. The public `PDF` renderer accepts only `data:` resources. Persisted report `HTML` and `PDF` files are withheld from public downloads, artifact archives and raw file routes, including reports saved before this policy. Owners and share recipients retain their saved reports and live input embeds. Derived outputs written by the agent may contain input data, so making a job public requires reviewing those outputs.
- **Admin access requires both authentication and admin status, re-validated at action time.** Admin pages are protected by stacked `@require_auth` and `@require_admin` decorators, and every admin action handler re-reads the caller's session and admin flag from the database before acting, so a logged-out, demoted or deleted admin loses the page's powers in an open tab. Admins cannot remove their own approval.

### Review Tokens

- **Tokens are hashed before storage.** Review tokens are generated with `secrets.token_urlsafe(32)` (256 bits of entropy) and stored as `SHA-256` hashes. The database never holds the plaintext.
- **Redemption behind a confirmation page.** The review link only renders a sign-in page. Signing in is a same-origin JSON POST from that page, so a cross-site link can show the page but never signs the visitor in.
- **Race-condition safe sign-in.** The sign-in lookup uses `SELECT ... FOR UPDATE` to prevent double-redemption, and a redeemed token whose reviewer account no longer exists is refused instead of minting a new one.
- **Referrer leakage prevention.** Sign-in responses set `Referrer-Policy: no-referrer` so the token doesn't leak in HTTP headers.
- **Review access lifetime.** Cookie and API key authentication check the redeemed review token on every request, so expiry blocks credentials and actions from open pages without a cleanup job. Revocation disables the reviewer account and deletes its sessions. Deleting a token creator disables the accounts their tokens redeemed, and deleting a reviewer revokes their token.

### Secrets Management

- **No secrets in code.** All credentials come from environment variables or TOML configuration, never from hardcoded values.
- **Credentials not logged.** A search for credential patterns in logging output found no evidence of API keys, tokens, or passwords being written to logs.
- **Per-process secret in error mode.** When the web starts the configuration-error page, it signs browser storage with a random per-process secret.

---

## What's Missing or Could Be Improved

### High

| Finding | Description | Recommended Next Step |
|---------|-------------|----------------------|
| **No rate limiting on most endpoints** | Only the health-check endpoint has rate limiting (10/minute via slowapi). Login, job creation, file upload, and token redemption endpoints have no throttling. | Extend rate limiting to all public-facing and authentication-related endpoints. |
| **No audit logging for admin actions** | Admin operations (user approvals, job reassignment, token creation/revocation) are logged to the application logger but not to a persistent, tamper-evident audit table. | Create an `audit_log` database table and record all admin actions with timestamps, actor, and details. |
| **No CSRF token validation** | While `samesite=lax` cookies provide partial protection, there is no explicit CSRF token for state-changing form submissions. | Add CSRF token generation and validation for all POST/PUT/DELETE operations. |
| **Database URL passed to agent containers via environment variables** | The database URL (including its password) is injected into agent containers as a plain environment variable. LLM provider credentials are not passed through; the web-side LLM proxy substitutes a per-run placeholder for them. If a container is compromised, the database URL is exposed. | Consider a secrets manager (e.g., HashiCorp Vault) or short-lived, scoped tokens instead of a long-lived database password in the environment. |
| **Base Docker image not pinned to digest** | Dockerfiles use `python:3.12-slim` without a SHA-256 digest pin, meaning a compromised or updated upstream image could silently change the build. | Pin to a specific image digest (e.g., `python:3.12-slim@sha256:abc...`). |

### Medium

| Finding | Description | Recommended Next Step |
|---------|-------------|----------------------|
| **No per-iteration timeout or token budget** | An individual agent iteration can run indefinitely and consume unlimited tokens. The only safety limit is the total iteration count (max 20). | Add a wall-clock timeout (e.g., 15 minutes) and a maximum token spend per iteration. |
| **Budget overshoot and unmetered calls** | The proxy checks configured job and project spend limits before each model call. At most four model calls per job can run across `HTTP` and websocket connections, with admission held through response cleanup and attempted cost recording. Excess calls receive `429`. Four accommodates a main Claude Code agent and three parallel subagents, following Anthropic's [parallel worker examples](https://code.claude.com/docs/en/agent-teams) and [independent subagent requests](https://code.claude.com/docs/en/sub-agents). | In-flight calls can overshoot spend limits. Aborted calls and missing usage are logged and release admission, so unknown provider charges remain outside the ledger bound. |
| **OpenAPI docs exposed without authentication** | `/api-docs`, `/api-redoc`, and `/openapi.json` are accessible to anyone, revealing the full API surface to potential attackers. | Require authentication for API documentation, or disable it in production. |
| **No automated dependency vulnerability scanning** | There is no evidence of Dependabot, Snyk, or similar tooling monitoring the 35+ direct dependencies for known CVEs. | Enable automated CVE scanning in CI (e.g., GitHub Dependabot, `pip-audit`, or Snyk). |
| **No secret key rotation mechanism** | Changing `OPENSCIENTIST_SECRET_KEY` invalidates all sessions and encrypted data. There is no graceful rotation workflow. | Implement key rotation support that re-encrypts data with the new key while still accepting the old key during a transition window. |
| **Job shares don't expire** | Once a job is shared with another user, access is permanent unless manually revoked. | Add optional expiry to job shares and a user-facing revocation interface. |
| **Review tokens don't require expiry** | Token expiry is optional. A never-expiring token that leaks could be used indefinitely. | Make expiry mandatory with a reasonable default (e.g., 7 days, maximum 1 year). |
| **No seccomp or AppArmor profiles** | Agent and executor containers rely only on `no-new-privileges`. No system-call filtering is applied. | Define seccomp profiles to restrict unnecessary syscalls in agent and executor containers. |

### Low

| Finding | Description | Recommended Next Step |
|---------|-------------|----------------------|
| **Email validation is minimal** | Email addresses are validated with a simple regex (`^[^@\s]+@[^@\s]+$`). Invalid formats like `a@b` pass validation. | Use a proper email validation library (e.g., `email-validator`). |
| **Agent code imports are permissive** | The allowed-imports list for Python code execution includes `requests` (network access) and `os` (environment access). | Review and tighten the import allowlist; consider removing `os` or restricting it to `os.path`. |
| **Research questions logged as-is** | Job titles and research questions appear in application logs without PII scrubbing. | Evaluate whether research context in logs is acceptable for your compliance requirements. |
| **Log files stored as plaintext** | Iteration logs and agent transcripts are written as unencrypted JSON and text files in the job directory. | Consider encrypting log files at rest if they may contain sensitive research content. |
| **Admin pages don't require reauthentication** | An attacker holding a still-valid admin session (for example a stolen cookie from an open browser) can perform admin actions without entering a password; action-time revalidation refuses only sessions that are no longer valid or no longer admin. | Require reauthentication (or a session timeout) before sensitive admin operations. |
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
| Container privileges | Good | Web runs as a dedicated non-root user in the jobs group; only the launcher holds the Docker socket |
| Dependency scanning | Needs Work | No automated CVE monitoring |
| Execution guardrails | Needs Work | No per-iteration timeout or token budget |
| Security headers | Strong | CSP, `X-Frame-Options`, `nosniff`, restrictive `Permissions-Policy`, HSTS over HTTPS |
