# RFC: Air-Gapped / Zero-Egress Mode

**Status:** Draft v2 — for review (no implementation yet). Revised after two adversarial Codex reviews; see §20 Revision Log.
**Date:** 2026-05-29
**Author:** Justin Reese (with Claude; reviews by OpenAI Codex)
**Related:** [`DESIGN.md`](DESIGN.md), [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md), [`DEPLOYMENT.md`](DEPLOYMENT.md)

---

## 1. Motivation

OpenScientist is increasingly pointed at sensitive data (patient phenopackets, unpublished
clinical/-omics datasets). Operators in regulated or classified environments need to run the agent
against that data with a **defensible guarantee that no unauthorized network connections occur from
the agent runtime** — not to the Anthropic API, not to NCBI/PubMed, not to any endpoint reachable
from agent-authored Python, and not via `curl`/`wget`/DNS tricks.

This RFC proposes an opt-in **air-gapped mode**. The default deployment is unchanged: normal
OpenScientist with cloud LLM and internet access. A tech-savvy operator who follows the documented
setup gets a deployment where unauthorized agent network egress is **physically prevented at the
network layer** and **demonstrably so** (per-job signed attestation).

The guarantee is precise (§4). **It is a network-connectivity guarantee, not a "nothing leaves the
box" guarantee** — the job's report and artifacts are designed export channels and are addressed
separately (§11). The earlier framing was rejected as unfalsifiable.

> **Dependency:** Luca's local-model provider refactor removes the largest egress channel — the LLM
> call itself. This RFC assumes that work lands and treats the local model as an *allowlisted, no-egress,
> attested* service on the internal network (§7). We do not design the local-LLM provider here; we
> design *around* it as an integration point (§7, §17).

---

## 2. Goals and Non-Goals

### Goals

- **G1 — Bounded, defensible guarantee.** No unauthorized network connections from agent or
  executor containers to anything outside an explicit, IP:port-allowlisted set of local services.
  Enforced by the kernel and host firewall, not by application code.
- **G2 — Attested, not asserted.** A **per-job** signed attestation record (network inspect, routes,
  resolver config, firewall snapshot, probe transcripts, image digests) is produced and stored
  alongside job artifacts; CI runs `make airgap-verify` as a regression gate.
- **G3 — Default-off.** Normal OpenScientist behavior is byte-for-byte unchanged when the mode is
  disabled. A single master switch flips everything.
- **G4 — Fail-closed.** If the mode is on but enforcement can't be established (network can't be made
  internal, host firewall not applied, required local service unreachable, IPv6 not disabled,
  cloud provider/credentials present), jobs and/or the server refuse to start with explicit errors.
  No silent fallback to bridge networking.
- **G5 — Offline literature.** A documented, supported path to run PubMed search against a local
  mirror with corpus integrity (snapshot IDs, hash manifests). Tooling + docs; not the data.
- **G6 — Output channel discipline.** The job's report and artifact bundle are explicit, audited
  *export* channels with declassification review, not network-level guarantees (§11).
- **G7 — We own the operator experience.** Makefile targets + thorough docs make a correct
  air-gapped install achievable by a competent operator without guesswork.

### Non-Goals

- **Not** defending against a malicious operator, host compromise, or Docker daemon compromise.
- **Not** eliminating covert/side channels (timing, resource, power).
- **Not** building the local LLM serving stack (Luca's refactor; we integrate).
- **Not** treating the job report/artifacts as a covered network channel — they are *operator-
  reviewed export artifacts*, governed by §11.

### Stance on the control plane

A previous draft listed the control-plane web server's own outbound connections as "out of scope."
Codex correctly pushed back: the agent can write fields the control plane then ships out (e.g.,
via ntfy notifications, pricing fetches, OAuth callbacks, skill sync). Air-gap mode therefore
**also disables the control plane's outbound integrations** when the master switch is on (§13). A
fully offline *deployment* additionally requires the operator to block the server container's
network at the host firewall — a deployment recommendation, not a code-level guarantee.

---

## 3. The Core Insight

You cannot get a guarantee by enumerating and disabling tools (`wget`, `lynx`, `requests`, SPARQL,
…). That is whack-a-mole. The guarantee comes from the network layer: agent/executor containers
have *no route off the box* — verified by the kernel, the routing table, and the host firewall.

But network-layer isolation alone is **necessary, not sufficient**:

- `internal: true` is the right Docker primitive, but a container can still reach the network
  gateway, the host (via `host-gateway` / `host.docker.internal`), other co-attached networks, or
  egress over IPv6 if not specifically denied.
- The agent today **mounts the Docker socket RW**, which lets it create *new* containers on any
  network — bypassing the agent's own isolation. This must be eliminated, not merely restricted (§9).
- Output channels (report, artifact ZIP) leave the box by design; they need their own discipline (§11).

So the design is **layered**: kernel routing (internal network) + host firewall (nftables/ip6tables
default-DROP, allowlist by IP:port) + Docker configuration (per-job network, no multi-attach, no
host-gateway, IPv6 disabled) + executor spawn architecture (no socket in agent; control-plane
spawn) + container hardening (caps, seccomp, AppArmor/SELinux, userns, read-only rootfs) + DiD
(repointed/disabled tools, gated SDK built-ins, credential minimization) + output discipline.

Tool repointing and disabling remain valuable as **defense-in-depth + fail-fast UX**, not as the
security boundary.

---

## 4. Threat Model

### Adversary

The **untrusted party is the agent itself** — an LLM that may be steered into exfiltration by
**prompt injection** in the material it processes (poisoned phenopacket fields, hostile paper
text, malicious skill content, attacker-controlled data values). We assume the agent may attempt,
with full creativity, to (a) open network connections to external hosts, (b) coerce allowlisted
local services into relaying, (c) encode data into outputs the operator later exports, (d) write
files that influence later runs, and (e) spawn sibling containers to bypass its own isolation.

The operator, host OS, kernel, and Docker daemon are **trusted**. The operator is also explicitly
the **declassification authority for outputs** (§11): they review reports/artifacts before
releasing them externally.

### Assets to protect (from unauthorized network egress)

The job's input data and any derived content (research question, data summaries, intermediate
analyses, findings, secrets reachable in the container environment) — none of it may reach an
unauthorized network destination from the agent runtime.

### In-scope egress vectors (must all be closed by the design)

| Vector | Today | Status in v2 |
|---|---|---|
| LLM API call | direct from container to Anthropic/Foundry/etc. | rejected at startup unless internal endpoint (§7) |
| PubMed search | hardcoded `https://eutils.ncbi.nlm.nih.gov/...` | repointed to internal mirror (§11) |
| Agent Python `requests`/sockets | allowed | network unreachable; `requests` removed from allowlist (§10) |
| SPARQL `# ENDPOINT:` queries | arbitrary | allowlisted to internal endpoints only (§10) |
| Shell tools (`curl`, `wget`, `lynx`) | unrestricted in image | network unreachable; offline-mode env defaults (§10) |
| DNS exfiltration | resolver reachable & forwarding | no external resolver; static hosts or non-recursive local resolver; unique-subdomain probes (§6.3) |
| **Sibling containers via Docker socket** | agent mounts socket RW → can `docker run --network bridge` | **socket removed from agent; executor spawn moves to control plane (§9)** |
| Skill ingestion | pulls from GitHub | disabled in mode; skills pre-bundled, signed (§10) |
| **Multi-network attachment, `host-gateway`, `extra_hosts`** | possible | forbidden in mode (§6.2) |
| **IPv6 egress** | possible if not explicitly dropped | disabled per network/container + ip6tables DROP (§6.4) |
| **NET_RAW / packet sockets** | default capability | `cap_drop=ALL` (§8) |
| **Control-plane outbound (ntfy, pricing, OAuth, skill sync)** | active | disabled in mode (§13) |
| **MCP `cwd=job_dir` + `.env` discovery** | agent can write `.env` the next subprocess may load | `.env` discovery disabled in agent/MCP/executor contexts (§10) |

### Out-of-scope egress paths covered by other mechanisms

- **Output channel (report/artifacts/provenance):** the operator will export these; they are not a
  network-layer concern. Governed by §11 (Export Boundary).
- **Allowlisted local services as sinks:** the local LLM and local PubMed mirror are addressed by
  service contracts (§7).
- **Credentials reachable in env:** addressed by credential minimization (§12).

### Explicitly OUT of scope

- Host or kernel or Docker daemon compromise; malicious operator; covert/side channels
  (timing, cache, power); the social-engineering channel where an operator clicks a link in a
  poisoned report (mitigated, but not eliminated, by §11's link/HTML stripping).

### Precise statement of the guarantee (please cite with §16 and §11)

> When air-gapped mode is enabled, all configured invariants (§5) are established at job start,
> the per-job attestation passes (§14), and the host's firewall policy is in effect, **no process
> inside an agent container or any container spawned for that job can open a network connection to
> any host other than the explicitly allowlisted local services (by IP and port), as enforced by
> Docker network configuration, host nftables/ip6tables, and the kernel's routing tables.** The
> guarantee is restricted to network-layer connectivity; it does not cover the operator-reviewed
> export of report/artifact files (§11), nor what an allowlisted local service does with the
> traffic it receives (§7's contracts), nor side channels.

---

## 5. Design Overview

A single master switch — **`OPENSCIENTIST_AIR_GAPPED`** (default `false`) — is the source of truth.
When `true`, it deterministically establishes all of the following invariants; **none is optional**.
Inability to establish any one of them is a fail-closed startup error (G4):

1. **Per-job internal Docker network** with no gateway/NAT (§6).
2. **Host firewall** (nftables + ip6tables) default-DROP from the per-job bridge, allowlist only the
   local LLM and PubMed services by **IP:port** (§6).
3. **IPv6 disabled** per network/container; `host-gateway`/`extra_hosts`/multi-network attachment
   forbidden; single network attachment per container (§6.2).
4. **DNS hardened**: no external recursive resolver; static `--add-host` mappings to the
   allowlisted services, or a local non-recursive allowlist resolver (§6.3).
5. **No Docker socket in agent or executor containers**; executor spawning moves to a trusted
   control-plane component (§9).
6. **Container hardening parity**: agent and executor are hardened identically — `cap_drop=ALL`,
   custom seccomp, AppArmor/SELinux, userns remap, no-new-privileges, read-only rootfs, pids
   limit, digest-pinned images (§8).
7. **Provider rejection**: cloud providers refused at startup; only providers whose endpoint
   resolves to the allowlisted internal LLM are accepted (§7).
8. **PubMed repointed** to internal mirror; `literature.py` base URL must be internal (§11).
9. **Defense-in-depth tool/code gating**: remove `requests` etc. from code-exec allowlist, SPARQL
   endpoint allowlist, network-touching MCP and SDK built-in tools disabled, `.env` discovery
   disabled in agent/MCP/executor contexts (§10).
10. **Skills pre-bundled, signed, and frozen** for the job; GitHub fetch disabled (§10).
11. **Credential minimization**: cloud keys, GitHub token, master secret, full DB URL stripped
    from agent env; job-scoped DB credentials; verifier fails if forbidden secrets are detected
    (§12).
12. **Control-plane outbound disabled** (ntfy, pricing, skill sync, OAuth callbacks) (§13).
13. **Export boundary** active: report/artifacts go through declassification review with
    link/HTML stripping (§11).
14. **Per-job signed attestation** produced before the agent starts and at job end (§14).

---

## 6. Network Isolation — Layered

### 6.1 Per-job internal Docker network (necessary baseline)

For each job, create a fresh network with `internal: true` and `enable_ipv6: false`. Containers on
it can reach each other; Docker installs no gateway/NAT — the kernel has no route off-net.

- Per-job (not shared): blast-radius reduction; lets policy be per-job; deletion on job end leaves
  no residue.
- Auto-detection fallback to `bridge` in `job_container/utils.py:resolve_docker_network` is
  **made fatal in air-gap mode** (G4).
- `network_mode=host`, `network_mode=container:<id>`, `macvlan`, `ipvlan`, and `network connect`
  during the job are **forbidden** (validated at startup and per-container).

### 6.2 Host firewall as the real enforcement

`internal: true` is *necessary but not sufficient* — it removes the default route but does not
itself drop traffic to the host or co-attached services. Air-gap mode therefore installs **host-level
nftables (and ip6tables) rules with default-DROP** on the per-job bridge, allowing only `IP:port`
flows to the local LLM and PubMed services. These rules are part of the attestation evidence (§14).

Additionally:
- **Forbid `host-gateway` and `extra_hosts`.** `host.docker.internal` resolves to the host (and on
  Docker Desktop, automatically) — explicitly disable, validate, and probe.
- **Single network attachment per container.** Multi-network attachment can route through a
  non-internal network; validated and probed.
- **Drop `NET_RAW`** (default capability) so the agent cannot use raw/packet sockets (§8).
- **Require a patched Docker Engine version** (CVE-2024-29018 leaked DNS externally on certain
  Moby versions even with internal networks). Pinned minimum version checked at startup.

### 6.3 DNS handling

DNS itself is an egress channel: even with all forwarding blocked, queries to a permissive resolver
can leak information through its logs (data encoded as subdomains). Air-gap mode:

- **No external recursive resolver.** Either no DNS at all and use `--add-host` for the allowlisted
  services, **or** a small local non-recursive resolver bound to the per-job network that answers
  only the allowlist and explicitly returns NXDOMAIN otherwise (with auditable logs).
- The embedded Docker DNS (127.0.0.11) is configured to never forward upstream.
- Attestation includes a **unique-subdomain probe** (random nonce) to demonstrate that exfil via
  DNS-encoded data does not reach any external server (§14).

### 6.4 IPv6

IPv6 is disabled per network and per container in air-gap mode. If a deployment requires v6, the
RFC's enforcement extends: ip6tables default-DROP plus explicit allow rules, plus IPv6 attestation
probes (TCP, UDP, ICMPv6). Default is **off**.

### 6.5 What `internal: true` does, exactly

For evidence in the attestation: `docker network inspect`, `ip route`, `ip -6 route`, and
`nft list ruleset` (or `iptables-save`) are captured per job. Reviewers can verify there is no
default route, no NAT, and DROP rules with the expected allowlist.

---

## 7. Allowlisted Local Services + Service Contracts

Two services live on each per-job internal network and are the **only** hosts the agent can reach:

1. **Local LLM endpoint** (Luca's local-model refactor).
2. **Local PubMed mirror** (§11).

Allowlisting is by IP:port at the host firewall, not by name or trust. In addition, each
allowlisted service must satisfy a **service contract** before air-gap mode considers it valid:

- **No outbound network.** The service container is also on the same per-job internal network with
  no other attachment; ip6tables/nftables block egress from it; this is attested.
- **No remote fallback.** Configuration disables any remote endpoint or fallback path (e.g., the
  local mirror must not call NCBI on cache miss; the local LLM must not call cloud endpoints).
- **Bounded logs.** Logs are size-bounded, rotated, written to a known location, and reviewable;
  the agent's job content does not appear in unbounded retained logs.
- **No write-back to job dir.** Service containers do not mount job directories writable.
- **Service-side attestation.** `airgap-verify` runs negative egress probes **from inside the
  service containers** as well as the agent (§14), so the contracts are checked, not trusted.

Cloud providers are rejected at startup: `providers/__init__.py:get_provider` is wrapped with a
mode-aware guard that requires the configured endpoint to resolve to the allowlisted internal LLM
IP.

---

## 8. Container Hardening (parity: agent and executor)

The agent and executor containers must be hardened **identically** — historically the executor has
been weaker (no cap-drop, `read_only=False`, default seccomp). Air-gap mode enforces, on both:

| Control | Notes |
|---|---|
| Per-job internal network only (§6.1) | the kernel boundary |
| Host firewall default-DROP + IP:port allowlist (§6.2) | the real enforcement |
| **No Docker socket** | OQ#1 resolved — Option A (§9) |
| `cap_drop=["ALL"]`, minimal explicit `cap_add` | drops `NET_RAW` and other defaults |
| Custom seccomp profile | minimal syscalls; closes `SECURITY_REVIEW.md` gap |
| AppArmor or SELinux profile | mandatory in air-gap mode |
| `no-new-privileges` | already present |
| User-namespace remap (`userns-remap`) | host-uid isolation |
| Non-root user (UID 1001) | already present |
| `read_only=True` rootfs + explicit tmpfs for `/tmp`, etc. | tampering & persistence |
| `pids_limit`, file-size limit, process/wall-clock timeouts | runaway containment |
| Digest-pinned base image (`Dockerfile.agent`, `Dockerfile.executor`) | closes SR gap; reproducible |
| `dns` set per §6.3 | no upstream resolver |
| `extra_hosts: []`, single network attachment | §6.2 invariants |

Failure to apply any of these is fail-closed at job start.

---

## 9. Executor Spawn Architecture — Open Question #1 Resolved (Option A)

**Decision: remove the Docker socket from agent containers; spawn executor containers from a
trusted control-plane component outside the agent's namespace.**

Rationale (from both Codex reviews):

- Option B (a Docker socket proxy that pins `--network=<internal>` and forbids overrides) is not
  workable in practice: the Docker API attack surface that must be blocked is too broad — bind
  mounts, `--privileged`, `--cap-add`, `--device`, host PID/IPC/UTS, `extra_hosts`, DNS,
  `network connect`, image `pull/build/load/import`, volume creation, `exec`/`cp`/`inspect` on
  other containers, plugins, container update. A proxy that gets any of these wrong is a single-step
  escape; an enumerable, default-deny proxy becomes a large new security boundary in its own right.
- Option A removes the socket entirely. The agent has no Docker API and cannot escape via sibling
  containers. The control plane is the sole spawn authority and applies the air-gap policy from
  trusted config.

**Architecture sketch:**

- Agent container: no `/var/run/docker.sock`, no Docker CLI in the image, no Docker GID. (Closes
  `SECURITY_REVIEW.md` Critical gap.)
- A small **executor-spawn service** runs in the control-plane trust domain. It receives requests
  from the agent via a constrained, validated channel (e.g., a Unix socket or an authenticated
  HTTP endpoint *on the per-job internal network*, exposing only `submit_code_execution`,
  `get_result`, `cancel`).
- That service spawns the executor container with **fixed image digest, fixed per-job internal
  network, fixed mounts, no override surface**. All policy is hardcoded; the agent cannot pass
  Docker flags through.
- This integrates with existing `ContainerManager` (`container_manager.py:81, 303-315`) by moving
  its `docker.from_env()` callsites into the spawn service.

Integration points: `openscientist_tools/code_exec.py` (dispatch becomes an RPC to the spawn
service rather than a local Docker call), `job_container/runner.py` (no socket bind-mount, no
docker-group), `container_manager.py` (factored).

This is now a release-blocking invariant for PR-1 (§18).

---

## 10. Defense-in-Depth (application layer)

Not the boundary (§3) — these make failures fast and legible:

- **PubMed:** `literature.py:37` base URL becomes `PUBMED_BASE_URL`; in air-gap mode it must be
  internal or the tool is disabled (§11).
- **Code execution import allowlist:** remove `requests` and other network libs from
  `code_executor.py:28-57`; document that import filtering is fail-fast UX, not enforcement.
- **SPARQL:** validate `# ENDPOINT:` against an allowlist (`code_executor.py:404-410`); air-gap
  mode allows only internal endpoints.
- **MCP tool gating:** extend the conditional-registration pattern (Phenix/Exomiser/hypotheses) to
  network-touching tools in air-gap mode. Integration: `openscientist_tools/server.py`.
- **SDK built-in tool gating:** Claude Code's SDK ships with its own tools (web fetch, etc.)
  *separate* from MCP-registered ones. Air-gap mode disables web/network-capable built-ins
  explicitly via the SDK options (`agent/claude_code_agent.py`).
- **Package managers:** `pip`, `uv`, `cargo`, `git` are configured offline by default in air-gap
  agent/executor images (no index URLs, no remotes). Attestation probes them (§14).
- **Skills:** pre-bundled at image-build time, signed, and **immutable for the duration of the
  job** (no DB-driven updates while jobs are in flight); GitHub ingestion path disabled.
- **MCP `cwd=job_dir` + `.env`:** `.env` discovery is disabled in agent/MCP/executor contexts so an
  agent-written `.env` in the job dir cannot reconfigure later subprocesses; trusted config is
  frozen before the agent runs.

---

## 11. Output / Export Boundary

The job's **report** and **artifact ZIP** are *designed* bulk export channels — they leave the box
when the operator downloads them. The network-layer guarantee does not cover them; they need their
own discipline.

In air-gap mode:

- **Reports are treated as untrusted output.** A renderer pass strips active HTML, external links,
  remote references, and inline scripts before display/export, so the operator does not become an
  unwitting relay by clicking a poisoned link.
- **Artifact ZIPs go through a declassification step.** A manifest is generated; the operator
  reviews and may exclude provenance/raw-data/log files; DLP hooks (regex-based scrubbing for
  obvious patterns — API key shapes, email addresses if configured, etc.) optionally apply. Audit
  log records who exported what.
- **Operator is the declassification authority.** This is explicit in the threat model (§4); the
  RFC does not claim the guarantee covers what the operator chooses to release.
- **Provenance separation.** Optionally, agent-controlled provenance content can be siloed and
  excluded from default export.

These are not network-layer controls; they are operator-experience controls that prevent the
operator from accidentally relaying agent-encoded data. They are **out of scope of the §4 precise
guarantee** but are required for air-gap mode to be honest about what it does.

---

## 12. Credential Minimization

Even with network egress blocked, secrets reachable in the agent environment can be embedded into
output and exported (§11). Air-gap mode therefore strips them.

**Rule:** in air-gap mode, the agent container env contains only the secrets it strictly needs to
do the job — which, with a local LLM and local PubMed on the internal network, may be **zero
cloud/GitHub/master secrets**.

Concretely:

- Strip: provider API keys (Anthropic, Vertex, Bedrock, Foundry, CBORG), `GITHUB_TOKEN`,
  `OPENSCIENTIST_SECRET_KEY`, full `DATABASE_URL` (with credentials), any service-account files.
- Replace with: a **job-scoped DB credential** (least-privilege, time-limited, scoped to the
  job's row(s)), and the internal-network hostnames/ports for LLM and PubMed.
- A **startup verifier** scans the agent container's environment and refuses to start the job if a
  forbidden secret is present. The verifier list is the source of truth.

Integration: `job_container/runner.py:61-78` (`_build_container_environment`),
`settings.py:322-397` (`ProviderSettings.get_container_env_vars`).

---

## 13. Control-Plane Egress in Air-Gap Mode

The agent can influence control-plane behavior via fields it writes (e.g., `job_meta`). Anything
the control plane then sends outbound is an indirect egress path. In air-gap mode the control
plane therefore also disables:

- **ntfy notifications** (`ntfy.py`).
- **Pricing/cost fetches** (`providers/pricing.py`).
- **Skill sync from GitHub** (skill ingestion path).
- **OAuth callbacks / external provider auth flows** for the duration of air-gap operation.
- Any other outbound integration introduced later — gated by the same master switch.

A **fully offline deployment** additionally blocks the server container's outbound network at the
**host firewall**. That is a deployment recommendation (documented in `docs/AIR_GAPPED.md`),
because we cannot enforce host firewall rules from inside the application. The §4 guarantee covers
the agent runtime; the deployment hardening covers the rest.

---

## 14. Verification & Attestation

### `make airgap-verify` (CI gate)

Launches a real agent container under the full air-gap policy and runs probes:

**Negative probes (must ALL fail / time out):**

- DNS resolve of an external domain (e.g. `example.com`)
- DNS resolve of a **unique random subdomain** (detects resolver logging / DNS-encoded exfil)
- TCP connect to public IPv4 (e.g. `1.1.1.1:443`)
- TCP/UDP/ICMP connect over IPv6 (if any v6 interface present)
- ICMP ping to public IPv4
- `python -c "import urllib.request; urllib.request.urlopen('https://example.com')"`
- `curl`/`wget` to an external URL
- `pip install requests`, `uv pip install requests`, `cargo fetch`, `git ls-remote` to a public
  remote
- `host.docker.internal` and the bridge gateway IP — must be unreachable
- Attempted Docker API operations from the agent (since there's no socket, these should fail at
  `connect`)

**Positive probes (must succeed):**

- Reach the local LLM endpoint by IP:port
- Reach the local PubMed mirror by IP:port

**Service-side probes (must ALL fail) — run from inside the LLM and PubMed containers:**

- Same negative probe set as above, demonstrating the service contracts (§7).

### Per-job signed attestation record

For every air-gap job, before the agent starts and at job end, the system produces a JSON
attestation containing:

- Master switch value, all derived invariants (§5).
- `docker network inspect` for the per-job network.
- `ip route`, `ip -6 route` inside the agent container.
- `nft list ruleset` (or `iptables-save -t filter`) for the per-job rules.
- Resolver config (`/etc/resolv.conf` and `extra_hosts`).
- Container image digests for agent, executor, LLM, PubMed.
- Engine version.
- Probe transcripts (from a job-start mini run of `airgap-verify`).
- Credential-minimization verifier output.

The record is signed (job-scoped key) and stored alongside the job artifacts. A deployment may
only call itself air-gapped if this passes per-job and `make airgap-verify` passes in CI.

---

## 15. Local PubMed (we own the path; we don't bundle the data)

OpenScientist provides the **tooling and documentation** to build a local PubMed without shipping
the corpus.

- `make download-pubmed` (long-running, resumable): pulls NCBI **MEDLINE annual baseline** + daily
  update files. The target prints size/time/disk requirements up front and is interruptible.
- **Corpus integrity:** the target produces a hash manifest and **snapshot ID** for the loaded
  corpus. The snapshot ID is **recorded into each job's metadata** so analyses are reproducible
  and the corpus is auditable.
- **Update mechanics:** documented offline process for applying daily updates, with new snapshot
  IDs.
- **Local service:** a thin shim that exposes an eutils-compatible API (`esearch`/`efetch`) on the
  per-job internal network, so the only `literature.py` change is the base URL. Alternative
  (Elasticsearch/Solr + adapter) is discussed in the guide.
- **Service contract:** the mirror satisfies §7 — no outbound, no remote fallback, bounded logs.
- **Repoint:** `PUBMED_BASE_URL` set to the internal service; air-gap mode validates it resolves
  internally.

`docs/AIR_GAPPED_PUBMED.md` will be the operator guide: prerequisites, disk/time budget, download,
load, serve, repoint, verify, update cadence.

---

## 16. Configuration Surface

All new settings default to non-air-gapped behavior (G3). Likely additions:

| Setting (env) | Section | Purpose |
|---|---|---|
| `OPENSCIENTIST_AIR_GAPPED` | new / `ContainerSettings` | master switch (default `false`) |
| `OPENSCIENTIST_AIRGAP_LLM_ADDR` | `ProviderSettings` | `IP:port` of the local LLM (allowlist entry) |
| `OPENSCIENTIST_AIRGAP_PUBMED_ADDR` | new / `LiteratureSettings` | `IP:port` of the local PubMed mirror |
| `PUBMED_BASE_URL` | new / `LiteratureSettings` | full URL the MCP tool targets |
| `OPENSCIENTIST_AIRGAP_SPARQL_ALLOW` | code-exec config | allowlist of internal SPARQL endpoints |
| `OPENSCIENTIST_AIRGAP_MIN_ENGINE` | new | minimum Docker Engine version (for CVE-2024-29018) |

Settings remain pydantic `BaseSettings` with the `@lru_cache` singleton (`settings.py`). The mode
exposes a single validated `air_gapped: bool` property; the rest of the code branches on it.

---

## 17. Integration Points (file-level)

Updated from the egress map (corrected path: tool server lives at `src/openscientist_tools/server.py`,
not `src/openscientist/openscientist_tools/server.py`):

- `job_container/runner.py:203-220` — per-job internal network, hardening flags, **no Docker
  socket mount, no docker-group** in air-gap mode.
- `job_container/utils.py:42-61` (`resolve_docker_network`) — **bridge fallback is fatal** in
  air-gap mode.
- `job_container/container_manager.py:81, 303-315` — refactor `docker.from_env()` callsites into
  the new control-plane executor-spawn service (§9).
- New: control-plane executor-spawn service + the agent-side RPC adapter (replacing direct Docker
  calls in `openscientist_tools/code_exec.py`).
- `providers/__init__.py:get_provider`, `settings.py:ProviderSettings` — cloud-provider rejection;
  require allowlisted internal LLM endpoint.
- `literature.py:37` — `PUBMED_BASE_URL`.
- `code_executor.py:28-57` (import allowlist), `:364-435` / `:404-410` (SPARQL allowlist).
- `openscientist_tools/server.py:18-26` — conditional MCP tool registration for air-gap.
- `agent/claude_code_agent.py:170-193` — SDK options gate web/network built-in tools; provider env
  injection minimized (§12).
- Skill ingestion path — disable GitHub fetch; pre-bundle + sign; immutable during job runs.
- `Dockerfile.agent`, `Dockerfile.executor` — digest pinning; offline-by-default package managers;
  AppArmor/seccomp/userns.
- Host firewall management (new): nftables/ip6tables rule application + teardown per job.
- Attestation writer (new): per-job signed JSON record.
- Output / export pipeline (new): report renderer hardening, artifact-pack declassification
  manifest.
- Control plane: `ntfy.py`, `providers/pricing.py`, skill ingestion, OAuth flows — all gated by
  air-gap mode (§13).
- `Makefile` — `download-pubmed`, `airgap-verify` targets.

---

## 18. Phased Implementation Plan (after RFC approval)

**PR 1 — Foundation + the guarantee (everything required to make G1 honest):**

- `OPENSCIENTIST_AIR_GAPPED` master switch + fail-closed startup checks.
- **Executor-spawn service** in the control plane; **socket removed from agent containers** (§9).
- Per-job internal network, no multi-attach, no `host-gateway`/`extra_hosts`, IPv6 disabled,
  bridge-fallback fatal (§6).
- Host firewall management (nftables + ip6tables, per-job rules, attestation).
- Container hardening parity for agent + executor (§8).
- DNS hardening (§6.3).
- Cloud-provider rejection; allowlisted local LLM endpoint (§7).
- **Credential minimization + verifier** (§12).
- **Control-plane outbound disabled in mode** (§13).
- **Output / export boundary** initial hardening (link/HTML stripping, basic manifest) (§11).
- `make airgap-verify` (negative + positive + service-side probes) and **per-job signed
  attestation** (§14). CI gate.

PR 1 is large because the guarantee can't be honestly claimed in parts. Anything less and we'd be
shipping the marketing without the substance.

**PR 2 — Local PubMed:** `PUBMED_BASE_URL` threading, `make download-pubmed`, eutils shim, snapshot
IDs, `docs/AIR_GAPPED_PUBMED.md`, mirror service contract.

**PR 3 — Defense-in-depth completions:** code-exec import tightening, SPARQL allowlist, MCP tool
gating, SDK built-in tool gating, MCP `cwd`/`.env` hardening, skills pre-bundling + signing,
package-manager offline mode.

**PR 4 — Operator deployment guide:** `docs/AIR_GAPPED.md` end-to-end, host-firewall recipe for a
fully offline deployment, integration with Luca's local-model work.

---

## 19. Remaining Open Questions

(The big one — Docker socket — is resolved in §9.)

1. **DNS architecture:** static `--add-host` versus a small local non-recursive resolver. Both
   work; the resolver is more flexible (and gives auditable logs) but adds a small service.
2. **Local LLM service contract details:** retention/log policy specifics that satisfy a regulator
   — coordinated with Luca.
3. **Local PubMed service tech:** thin eutils shim vs ES/Solr + adapter — bounded by how much of
   PubMed's query surface the agent actually uses (worth measuring on the existing job corpus).
4. **DLP scrubbing in export:** how aggressive a default? Regex policies for API keys/emails/etc.
   are opinionated.
5. **Engine version pin:** the minimum patched Engine version (CVE-2024-29018) — pick at
   implementation time.
6. **Job-scoped DB credentials:** generation and lifecycle (Postgres roles vs short-lived JWTs).

---

## 20. Residual Risk (honesty statement)

Air-gap mode prevents **unauthorized network connections** from agent and executor containers,
verified by per-job attestation and the host firewall. It does **not** address:

- The **report and artifact export channel** — operator-reviewed and operator-released; covered
  by §11 controls but ultimately the operator's responsibility.
- **What allowlisted local services do with the traffic they receive** — covered by §7 service
  contracts but rests on the operator standing those services up faithfully.
- **Indirect channels** via operator interaction with poisoned outputs (e.g., clicking a link in a
  report stripped of active HTML but not of plain text URLs the operator might copy).
- **Host, kernel, or Docker daemon compromise; malicious operator; physical access.**
- **Covert/side channels** (timing, cache, power, resource consumption).
- The **control-plane server's own outbound connections** when run with air-gap mode but without
  an additional host-firewall block (the recommendation in §13 closes this for a fully offline
  deployment, but it is an operator action, not a code-level guarantee).

The "guarantee" claim should always be cited with the §4 precise statement and this section. The
mode delivers a strong, attested, defensible network-layer guarantee — not a panacea.

---

## 21. Revision Log

**v2 (2026-05-29) — after Codex isolation-depth and red-team reviews:**

- **Narrowed the guarantee** from "nothing leaves the box / zero data exfiltration" to "no
  unauthorized network connections from agent/executor containers" (§1, §2, §4, §20).
- **Resolved OQ#1** to Option A: socket removed from agent containers; executor spawn moves to
  trusted control-plane service (§9). Updated phasing accordingly (§18).
- **Layered network controls** beyond `internal: true`: per-job internal network, host
  nftables/ip6tables default-DROP with IP:port allowlist, forbid `host-gateway`/`extra_hosts`/
  multi-network attachment, IPv6 disabled, bridge-fallback fatal, Engine version pin for
  CVE-2024-29018, drop `NET_RAW` (§6).
- **DNS handling**: no external recursive resolver; static `--add-host` or local non-recursive
  resolver; unique-subdomain probe (§6.3).
- **Service contracts** for allowlisted local LLM and PubMed (§7); service-side attestation probes
  (§14).
- **Container hardening parity** for agent and executor; userns, AppArmor/SELinux, pids limit,
  custom seccomp, digest pin both images (§8).
- **NEW §11 Output / Export Boundary**: report and artifact ZIPs as designed export channels;
  declassification review; HTML/link stripping; operator as declassification authority.
- **NEW §12 Credential Minimization**: cloud keys, GitHub token, master secret, full DB URL
  stripped from agent env; startup verifier.
- **NEW §13 Control-Plane Egress in Air-Gap Mode**: disable ntfy, pricing, skill sync, OAuth
  callbacks; host-firewall recommendation for fully offline deployment.
- **Attestation expanded** (§14): IPv6 + ICMP + unique-subdomain DNS + host-gateway + Docker API
  probes; **per-job signed JSON attestation** with network inspect, routes, firewall ruleset,
  resolver config, image digests, probe transcripts; service-side probes.
- **DiD additions** (§10): SDK built-in tool gating; package-manager offline mode;
  MCP `cwd=job_dir` `.env` hardening; skills signed and frozen.
- **Threat model revised** (§4): output channel scoped to §11; operator is declassification
  authority; precise claim narrowed.
- **Phasing revised** (§18): PR-1 now includes all prerequisites for the guarantee (socket
  removal, control-plane spawn, host firewall, IPv6, DNS, credential minimization, control-plane
  egress disable, export-boundary basics). The guarantee is shipped whole or not at all.
- **Path corrected** (§17): `src/openscientist_tools/server.py` (no leading `src/openscientist/`).
- **Residual risk expanded** (§20) with output channel, service-trust, indirect channels,
  control-plane caveats.
