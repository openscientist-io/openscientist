# RFC: Air-Gapped / Zero-Egress Mode

**Status:** Draft v3 — for review (no implementation yet). Revised after three adversarial Codex reviews; see §21 Revision Log.
**Date:** 2026-06-04
**Author:** Justin Reese (with Claude; reviews by OpenAI Codex)
**Related:** [`DESIGN.md`](DESIGN.md), [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md), [`DEPLOYMENT.md`](DEPLOYMENT.md)

---

## 1. Motivation

OpenScientist is increasingly pointed at sensitive data (patient phenopackets, unpublished
clinical/-omics datasets). Operators in regulated or classified environments need to run the agent
against that data with a **defensible guarantee that no unauthorized network connections occur from
the agent runtime** — not to the Anthropic API, not to OpenAI, not to NCBI/PubMed, not to any
endpoint reachable from agent-authored Python, and not via `curl`/`wget`/DNS tricks.

This RFC proposes an opt-in **air-gapped mode**. The default deployment is unchanged. A tech-savvy
operator who follows the documented setup gets a deployment where unauthorized agent network egress
is **physically prevented at the network layer** and **demonstrably so** (per-job signed
attestation).

The guarantee is precise (§4). **It is a network-connectivity guarantee, not a "nothing leaves the
box" guarantee** — the report and artifacts are designed export channels and are addressed
separately (§11).

> **Two agent backends are in scope.** OS now ships two agent backends — `ClaudeCodeAgent`
> (Claude Code SDK as subprocess) and `CodexAgent` (OpenAI's `codex` CLI as subprocess) — with a
> two-axis hierarchy: each `Provider` declares which backend family/families it's compatible with,
> and `agent/factory.py` dispatches accordingly. Both backends spawn their own subprocess inside
> the agent container and each carries its own config / auth surface. The network-layer guarantee
> (§6) holds uniformly for both; the configuration-level enforcement (§7, §8, §10, §12) needs
> per-backend treatment, called out where it diverges.

---

## 2. Goals and Non-Goals

### Goals

- **G1 — Bounded, defensible guarantee.** No unauthorized network connections from agent or
  executor containers (regardless of agent backend) to anything outside an IP:port-allowlisted set
  of local services. Enforced by the kernel and host firewall, not by application code.
- **G2 — Attested, not asserted.** A per-job signed attestation record (network inspect, routes,
  resolver config, firewall snapshot, probe transcripts, image digests) is produced and stored
  alongside job artifacts; CI runs `make airgap-verify` as a regression gate.
- **G3 — Default-off.** Normal OpenScientist behavior is byte-for-byte unchanged when the mode is
  disabled. A single master switch flips everything.
- **G4 — Fail-closed.** If the mode is on but enforcement can't be established (network can't be
  made internal, host firewall not applied, required local service unreachable, IPv6 not disabled,
  cloud provider/credentials present, Codex CLI network access not disabled), jobs and/or the
  server refuse to start with explicit errors. No silent fallback.
- **G5 — Offline literature.** A documented, supported path to run PubMed search against a local
  mirror with corpus integrity (snapshot IDs, hash manifests).
- **G6 — Output channel discipline.** The job report and artifact bundle are explicit, audited
  *export* channels with declassification review (§11). The `.codex/` config and auth artifacts
  the Codex backend writes into the job dir are subject to the same review.
- **G7 — Operator experience.** Makefile targets + thorough docs make a correct air-gapped install
  achievable without guesswork.

### Non-Goals

- **Not** defending against a malicious operator, host compromise, or Docker daemon compromise.
- **Not** eliminating covert/side channels (timing, resource, power).
- **Not** building the local LLM serving stack — the operator stands up Anthropic-compatible or
  OpenAI-compatible endpoints on the internal network and points OS at them.
- **Not** treating the report/artifacts as a covered network channel — operator-reviewed export.

### Stance on the control plane

A previous draft listed the control-plane web server's own outbound connections as out of scope.
Codex pushed back: the agent can write fields the control plane then ships out (ntfy notifications,
pricing fetches, OAuth callbacks, skill sync). Air-gap mode therefore **also disables the control
plane's outbound integrations** when the master switch is on (§13). A fully offline *deployment*
additionally requires the operator to block the server container's network at the host firewall —
a deployment recommendation, not a code-level guarantee.

---

## 3. The Core Insight

You cannot get a guarantee by enumerating and disabling tools (`wget`, `lynx`, `requests`, SPARQL,
…). That is whack-a-mole. The guarantee comes from the network layer: agent/executor containers
have *no route off the box* — verified by the kernel, the routing table, and the host firewall.

Network-layer isolation alone is **necessary, not sufficient**:

- `internal: true` is the right Docker primitive, but a container can still reach the network
  gateway, the host (via `host-gateway` / `host.docker.internal`), other co-attached networks, or
  egress over IPv6 if not specifically denied.
- The agent today mounts the Docker socket RW, which lets it create *new* containers on any
  network — bypassing its own isolation. This must be eliminated, not merely restricted (§9).
- **Each agent backend has its own subprocess with its own config/auth surface.** The Codex
  backend writes a `config.toml` from the parent env and reads `auth.json` — both currently land
  in the job dir and reproduce credentials there. This is a credential-exfil path the network
  guarantee alone doesn't close (§12).
- Output channels (report, artifact ZIP, `.codex/*` files) leave the box by design; they need their
  own discipline (§11).

The design is **layered**: kernel routing (internal network) + host firewall (default-DROP, IP:port
allowlist) + Docker configuration (per-job network, no multi-attach, no host-gateway, IPv6 off) +
executor spawn architecture (no socket in agent; control-plane spawn) + container hardening (caps,
seccomp, AppArmor/SELinux, userns, read-only rootfs) + per-backend application-layer enforcement
(`network_access_enabled=False` for Codex, gated SDK built-ins for Claude, repointed/disabled tools
for both, credential minimization) + output discipline.

---

## 4. Threat Model

### Adversary

The **untrusted party is the agent itself** — an LLM (driven by either backend) that may be steered
into exfiltration by **prompt injection** in the material it processes. We assume the agent may
attempt, with full creativity, to (a) open network connections to external hosts via any subprocess
it controls, (b) coerce allowlisted local services into relaying, (c) encode data into outputs the
operator later exports, (d) write files that influence later runs, (e) spawn sibling containers to
bypass its own isolation, and (f) **leverage its agent backend's subprocess** (Claude Code SDK or
`codex` CLI) — including the config/auth files those subprocesses read and the built-in tools they
expose (Codex's web search, Claude's web fetch) — to reach the network.

The operator, host OS, kernel, and Docker daemon are **trusted**. The operator is also explicitly
the **declassification authority for outputs** (§11).

### In-scope egress vectors (must all be closed)

| Vector | Today | Status in v3 |
|---|---|---|
| Claude SDK LLM call | direct from container to Anthropic/Foundry/etc. | rejected at startup unless internal endpoint (§7) |
| **Codex CLI LLM call** | **direct from container to OpenAI / Azure OpenAI** | **rejected at startup unless internal endpoint (§7); `network_access_enabled=False` (§8)** |
| **Codex CLI built-in web search** | **enabled by default** | **`web_search_enabled=False` required in airgap (§10)** |
| PubMed search | hardcoded `https://eutils.ncbi.nlm.nih.gov/...` | repointed to internal mirror (§15) |
| Agent Python `requests`/sockets | allowed | network unreachable; `requests` removed from allowlist (§10) |
| SPARQL `# ENDPOINT:` queries | arbitrary | allowlisted to internal endpoints only (§10) |
| Shell tools | unrestricted | network unreachable; offline-mode env defaults (§10) |
| DNS exfiltration | resolver reachable & forwarding | no external resolver; static hosts or local non-recursive resolver; unique-subdomain probes (§6.3) |
| Sibling containers via Docker socket | RW-mounted | socket removed from agent; executor spawn in control plane (§9) |
| Skill ingestion from GitHub | active | disabled in mode; skills pre-bundled, signed (§10) |
| Multi-network attachment, `host-gateway`, `extra_hosts` | possible | forbidden (§6.2) |
| IPv6 egress | possible if not dropped | disabled per network/container + ip6tables DROP (§6.4) |
| NET_RAW / packet sockets | default cap | `cap_drop=ALL` (§8) |
| Control-plane outbound (ntfy, pricing, OAuth, skill sync) | active | disabled (§13) |
| MCP `cwd=job_dir` `.env` discovery | possible | disabled (§10) |
| **Codex CLI config (`job_dir/.codex/config.toml`)** | **written from copy of parent env (contains DB URL, master secret, provider creds)** | **constructed from allowlist, not full env (§12)** |
| **Codex CLI auth (`job_dir/.codex/auth.json`)** | **may be copied from host with `0644` in `0777` parent dir** | **mounted as read-only tmpfs secret; never written to job dir (§12)** |
| **Codex CLI binary supply chain** | **downloaded from GitHub release at image-build time, no checksum** | **SHA256-pinned, pre-bundled in agent image (§8)** |

### Out-of-scope (covered separately)

- **Output channel** (report, artifacts, `.codex/*` files): operator-reviewed export → §11.
- **Allowlisted local services as sinks**: §7 service contracts.
- **Credentials reachable in env**: §12 credential minimization.

### Out of scope entirely

- Host or kernel or Docker daemon compromise; malicious operator; covert/side channels.

### Precise guarantee (cite with §16 and §11)

> When air-gapped mode is enabled, all invariants (§5) are established at job start, the per-job
> attestation passes (§14), and the host's firewall policy is in effect, **no process inside an
> agent container or any container spawned for that job — regardless of which agent backend
> (`ClaudeCodeAgent` or `CodexAgent`) the job uses — can open a network connection to any host
> other than the explicitly allowlisted local services (by IP and port), as enforced by Docker
> network configuration, host nftables/ip6tables, and the kernel's routing tables.** Restricted to
> network-layer connectivity; does not cover operator-reviewed export of report/artifact files
> (§11), what an allowlisted local service does with traffic it receives (§7 contracts), or side
> channels.

---

## 5. Design Overview

A single master switch — **`OPENSCIENTIST_AIR_GAPPED`** (default `false`) — is the source of truth.
When `true`, it deterministically establishes all of the following invariants; **none is optional**.
Inability to establish any one of them is fail-closed startup error (G4):

1. **Per-job internal Docker network** with no gateway/NAT (§6).
2. **Host firewall** (nftables + ip6tables) default-DROP from the per-job bridge, allowlist only
   the local LLM endpoint(s) and PubMed services by **IP:port** (§6).
3. **IPv6 disabled**; `host-gateway`/`extra_hosts`/multi-network forbidden; single network per
   container (§6.2).
4. **DNS hardened**: no external recursive resolver; static `--add-host` or local non-recursive
   allowlist resolver (§6.3).
5. **No Docker socket** in agent or executor containers; executor spawning moves to a trusted
   control-plane component (§9).
6. **Container hardening parity** — agent and executor identically hardened (caps, seccomp,
   AppArmor/SELinux, userns, no-new-privileges, read-only rootfs, pids limit, digest pinning) (§8).
7. **Provider-family endpoint validation.** Every `Provider` (regardless of agent backend)
   implements an `airgap_egress_targets()` contract that returns the deterministic IP:port set the
   provider will talk to. Air-gap startup walks the configured provider's egress set and refuses
   to run if any target doesn't resolve to the allowlisted internal LLM endpoint. Providers whose
   endpoint can't be made deterministic at startup (Bedrock/Vertex regional SDK clients without
   explicit override) are refused unless explicitly mapped to internal DNS/IP (§7).
8. **Codex CLI subprocess configured for no egress** — when the selected backend is Codex,
   `network_access_enabled=False` and `web_search_enabled=False` are required in the CodexAgent's
   `ThreadOptions`; the generated `config.toml` is built from an env allowlist, not a copy of the
   parent env (§7, §8, §12).
9. **PubMed repointed** to internal mirror (§15).
10. **Defense-in-depth tool/code gating** (§10): `requests` etc. removed from code-exec allowlist,
    SPARQL endpoint allowlist, network-touching MCP and SDK built-in tools (Claude *and* Codex)
    disabled, `.env` discovery disabled in agent/MCP/executor contexts.
11. **Skills pre-bundled, signed, frozen** for the job; GitHub fetch disabled (§10).
12. **Credential minimization**: cloud provider keys (Anthropic, Vertex, Bedrock, Foundry, CBORG,
    **OpenAI**, **Azure OpenAI**), GitHub token, master secret, full DB URL stripped from agent
    env; **Codex auth (`auth.json`) never written to job dir**; verifier fails if forbidden secrets
    appear in env, `.codex/config.toml`, or `.codex/auth.json` (§12).
13. **Control-plane outbound disabled** (ntfy, pricing, skill sync, OAuth) (§13).
14. **Export boundary** active: report and artifact ZIP pass through declassification review with
    link/HTML stripping; **`.codex/*` artifacts excluded from default export and scanned for
    secrets** (§11).
15. **Per-job signed attestation** produced before and after the agent run (§14).

---

## 6. Network Isolation — Layered

### 6.1 Per-job internal Docker network (necessary baseline)

For each job, create a fresh network with `internal: true` and `enable_ipv6: false`. Per-job (not
shared) for blast-radius reduction. The auto-detection fallback to `bridge` in
`job_container/utils.py:resolve_docker_network` is **made fatal in air-gap mode**. `network_mode`
of `host`, `container:<id>`, `macvlan`, `ipvlan`, and `network connect` during the job are
**forbidden** (validated at startup and per-container).

### 6.2 Host firewall as the real enforcement

`internal: true` is *necessary but not sufficient* — it removes the default route but doesn't drop
traffic to the host or co-attached services. Air-gap mode installs **host-level nftables (and
ip6tables) rules with default-DROP** on the per-job bridge, allowing only IP:port flows to the
local LLM and PubMed services. These rules are part of the attestation evidence (§14).

- **Forbid `host-gateway` and `extra_hosts`.** `host.docker.internal` resolves to the host (and on
  Docker Desktop, automatically) — disable, validate, probe.
- **Single network attachment** per container.
- **Drop `NET_RAW`** so the agent cannot use raw/packet sockets.
- **Require patched Docker Engine version** (CVE-2024-29018 leaked DNS externally on certain Moby
  versions even with internal networks).

### 6.3 DNS handling

DNS itself is an egress channel: even with all forwarding blocked, queries to a permissive resolver
can leak via subdomain encoding. Air-gap mode:

- No external recursive resolver. Either no DNS + `--add-host` for the allowlist, or a small local
  non-recursive resolver bound to the per-job network that answers only the allowlist and returns
  NXDOMAIN otherwise (with auditable logs).
- Embedded Docker DNS (127.0.0.11) configured to never forward upstream.
- Unique-subdomain probe (random nonce) in attestation to verify no DNS-encoded exfil reaches
  external (§14).

### 6.4 IPv6

Disabled per network and per container. If a deployment requires v6, ip6tables default-DROP plus
explicit allow + IPv6 attestation probes. Default off.

### 6.5 Evidence

Per-job attestation captures `docker network inspect`, `ip route`, `ip -6 route`, and
`nft list ruleset` (or `iptables-save`).

---

## 7. Allowlisted Local Services + Service Contracts

Two services live on each per-job internal network and are the **only** hosts the agent can reach:

1. **Local LLM endpoint(s).** Operator-stood-up; one or more depending on which backend the job
   uses (Anthropic-compatible for Claude jobs, OpenAI-compatible for Codex jobs, or both for a
   mixed deployment).
2. **Local PubMed mirror** (§15).

### 7.1 The two LLM-call paths

Both agent backends consume from the same allowlist but reach it via different subprocesses with
different configuration surfaces:

| | `ClaudeCodeAgent` | `CodexAgent` |
|---|---|---|
| Subprocess | Claude Code SDK | OpenAI `codex` CLI |
| Provider families | `ClaudeCompatible` (Anthropic, Bedrock, Vertex, Foundry, CBORG) | `CodexCompatible` (OpenAIDirect, Azure OpenAI) |
| Endpoint config | per-provider env (`ANTHROPIC_BASE_URL`, `ANTHROPIC_FOUNDRY_BASE_URL`, etc.) | Codex `config.toml` providers section, generated by `CodexAgent._write_codex_config` |
| Network controls | gated SDK built-in tools (web fetch, etc.) | `ThreadOptions.network_access_enabled` + `web_search_enabled` |
| Auth surface | env-injected provider creds | `auth.json` (currently host file or `$CODEX_HOME`) |

The kernel-level network boundary (§6) constrains both equally. The configuration-level enforcement
diverges and must be parallel — every clause below applies to *each backend's call path*.

### 7.2 Provider-family endpoint contract (`airgap_egress_targets()`)

Earlier drafts had a single check "the configured provider endpoint resolves to the internal LLM."
That worked when there was one chokepoint (`providers/__init__.py:get_provider`) and a handful of
providers. Now each provider derives its endpoint differently:

| Provider | Endpoint construction | Air-gap-checkable? |
|---|---|---|
| CBORG | `ANTHROPIC_BASE_URL` env override → explicit URL | yes |
| Foundry (Anthropic-on-Azure) | derives from `ANTHROPIC_FOUNDRY_RESOURCE` or override URL | yes (deterministic from settings) |
| OpenAIDirect | Codex CLI's default OpenAI provider/endpoint | yes (Codex CLI provider config) |
| Azure OpenAI | derives `https://{resource}.openai.azure.com/openai/v1` from `AZURE_OPENAI_RESOURCE` | yes (deterministic from settings) |
| Anthropic (direct) | default `api.anthropic.com` unless `ANTHROPIC_BASE_URL` overridden | yes only if `ANTHROPIC_BASE_URL` set |
| Bedrock | AWS SDK regional client, no explicit URL override surface | **no** — must be refused unless mapped via SDK endpoint resolver to internal |
| Vertex | Google SDK regional client, no explicit URL override surface | **no** — same |

The RFC therefore requires:

- A `Provider.airgap_egress_targets() -> set[(host, port)]` contract added to
  `providers/base.py` (or sibling mixin). Each provider implements it, returning the deterministic
  IP:port set its requests would target. Providers whose endpoint can't be made deterministic at
  startup raise `AirGapUnsupportedError` from this method.
- Air-gap startup calls `airgap_egress_targets()` on the configured provider; every returned
  `(host, port)` must resolve to an IP in the allowlist or the run is refused. The check is
  per-provider, not per-call-site.
- Providers that raise `AirGapUnsupportedError` are refused unless the operator explicitly maps
  them to internal endpoints via a settings override (e.g. `OPENSCIENTIST_AIRGAP_BEDROCK_ENDPOINT`
  mapped to an internal Bedrock-compatible service).

### 7.3 Service contracts

Each allowlisted service must satisfy a service contract before air-gap mode considers it valid:

- **No outbound network.** Service container is also on the same per-job internal network with no
  other attachment; ip6tables/nftables block egress from it; attested.
- **No remote fallback.** Configuration disables any remote endpoint or fallback path (the local
  mirror must not call NCBI on cache miss; the local LLM must not call cloud).
- **Bounded logs.** Size-bounded, rotated, written to a known location, reviewable; agent's job
  content does not appear in unbounded retained logs.
- **No write-back to job dir.**
- **Service-side attestation.** `airgap-verify` runs negative egress probes from *inside* the
  service containers as well as the agent.

---

## 8. Container Hardening (parity: agent and executor)

Agent and executor containers must be hardened **identically**. Air-gap mode enforces, on both:

| Control | Notes |
|---|---|
| Per-job internal network (§6.1) | kernel boundary |
| Host firewall default-DROP + IP:port allowlist (§6.2) | real enforcement |
| **No Docker socket** | OQ#1 resolved — Option A (§9) |
| `cap_drop=["ALL"]`, minimal explicit `cap_add` | drops `NET_RAW` |
| Custom seccomp profile | minimal syscalls |
| AppArmor or SELinux profile | mandatory |
| `no-new-privileges` | already present |
| User-namespace remap | host-uid isolation |
| Non-root user | already present |
| `read_only=True` rootfs + explicit tmpfs | tampering / persistence |
| `pids_limit`, file-size limit, process/wall-clock timeouts | runaway containment |
| Digest-pinned base images | reproducible |
| `dns` set per §6.3 | no upstream resolver |
| `extra_hosts: []`, single network attachment | §6.2 invariants |

### 8.1 Codex backend supply-chain hardening

The agent image currently installs `openai-codex-sdk` from PyPI and downloads the Codex CLI binary
from GitHub release URLs at image-build time. The installer call does not pin or verify a SHA256.
This is a build-time supply-chain hole that the RFC must close:

- The Codex CLI binary is **pre-bundled in the agent image** at a SHA256-pinned digest. Image build
  in air-gap mode forbids networked install of `openai-codex-sdk` or the Codex CLI; both come from
  a locally-vendored bundle.
- The Codex CLI binary's digest is captured in the per-job attestation (§14) alongside other image
  digests.
- A `make download-codex` (or equivalent) target documents the trusted download + SHA256
  verification step the operator runs once at provisioning time. Build-time fetch from
  GitHub-release URLs is rejected in air-gap mode.

### 8.2 Codex backend runtime hardening

The Codex backend currently sets `sandbox_mode="danger-full-access"` and `approval_policy="never"`
(`codex_agent.py:175-192`) but does *not* set `network_access_enabled=False` or
`web_search_enabled=False`. The SDK supports both options and emits Codex CLI config flags for
them. Air-gap mode requires:

```python
# CodexAgent ThreadOptions in air-gap mode (sketch)
ThreadOptions(
    sandbox_mode="danger-full-access",      # network is gated at OS level (§6); this is fine
    approval_policy="never",
    network_access_enabled=False,            # NEW — required in airgap
    web_search_enabled=False,                # NEW — required in airgap
    # ...
)
```

The presence of both flags is verified by the per-job attestation (the generated Codex CLI command
line / config is inspected for these flags).

### 8.3 Codex env construction

`CodexAgent._mcp_env()` currently copies the **entire parent process env** into the subprocess and
serializes it into `job_dir/.codex/config.toml`. That includes `DATABASE_URL`,
`OPENSCIENTIST_SECRET_KEY`, provider keys, and the executor image reference. In air-gap mode, the
Codex MCP env must be constructed from an **explicit allowlist** of variables the Codex CLI
actually needs — no copy-all (§12).

---

## 9. Executor Spawn Architecture — Open Question #1 Resolved (Option A)

**Decision (unchanged from v2): remove the Docker socket from agent containers; spawn executor
containers from a trusted control-plane component outside the agent's namespace.**

Option B (a Docker socket proxy that pins network and forbids overrides) is not workable: the API
attack surface that must be blocked is too broad (`--privileged`, bind mounts, `--cap-add`,
`--device`, host PID/IPC/UTS, `extra_hosts`, DNS, `network connect`, image `pull/build/load/import`,
volume creation, `exec`/`cp`/`inspect`, plugins, container update). A proxy that misses any of
these is a single-step escape.

**Architecture sketch:**

- Agent container: no `/var/run/docker.sock`, no Docker CLI in the image, no Docker GID.
- A small **executor-spawn service** runs in the control-plane trust domain. It receives requests
  from the agent via a constrained, validated channel (Unix socket or authenticated HTTP endpoint
  on the per-job internal network) exposing only `submit_code_execution`, `get_result`, `cancel`.
- That service spawns the executor container with **fixed image digest, fixed per-job internal
  network, fixed mounts, no override surface**. All policy hardcoded; the agent cannot pass Docker
  flags through.

> **Current state:** today `openscientist_tools/code_exec.py:129-161` calls
> `ContainerManager.execute_code()` directly. The RFC's executor-spawn service is **future work**
> required by PR-1; it does not yet exist. §17 reflects this honestly.

This is a release-blocking invariant for PR-1 (§18).

---

## 10. Defense-in-Depth (application layer)

Not the boundary (§3) — these make failures fast and legible:

### 10.1 Backend-agnostic

- **PubMed:** `literature.py:37` `base_url` becomes `PUBMED_BASE_URL`; in air-gap mode it must be
  internal or the tool is disabled (§15).
- **Code execution import allowlist:** remove `requests` and other network libs from
  `code_executor.py:27-57`; document as fail-fast UX, not enforcement.
- **SPARQL:** validate `# ENDPOINT:` against an allowlist (`code_executor.py:398-436`); no
  allowlist exists today — must be added.
- **MCP tool gating:** extend the conditional-registration pattern (currently only hypothesis
  tools at `openscientist_tools/knowledge.py:146-150`) to all network-touching tools in air-gap
  mode. Integration: `openscientist_tools/server.py:18-25`.
- **Package managers:** `pip`, `uv`, `cargo`, `git` configured offline by default in agent /
  executor images (no index URLs, no remotes). Attestation probes them (§14).
- **Skills:** pre-bundled, signed, immutable for the duration of the job; GitHub ingestion path
  disabled.
- **MCP `cwd=job_dir` `.env`:** discovery disabled in agent/MCP/executor contexts so an
  agent-written `.env` in the job dir cannot reconfigure later subprocesses.

### 10.2 Claude Code SDK backend specifics

- **SDK built-in tool gating.** Claude Code's SDK ships with its own tools (web fetch, etc.)
  separate from MCP-registered ones. Air-gap mode disables web/network-capable built-ins
  explicitly via the SDK options (`agent/claude_code_agent.py:170-193`).

### 10.3 Codex CLI backend specifics

- **Codex's built-in web search** is the analog of Claude SDK's web fetch. Required off in air-gap
  via `web_search_enabled=False` in `ThreadOptions` (`agent/codex_agent.py:166-192`).
- **Codex's `network_access_enabled=False`** required in `ThreadOptions` (§8.2).
- **Codex MCP env allowlist construction** (§8.3, §12).
- **Codex auth handling** (§12): never written to `job_dir/.codex/auth.json`; mounted as read-only
  tmpfs-backed secret instead, scoped to the subprocess.

---

## 11. Output / Export Boundary

The job's **report** and **artifact ZIP** are *designed* bulk export channels. They leave the box
when the operator downloads them. The network-layer guarantee does not cover them.

In air-gap mode:

- **Reports are treated as untrusted output.** Renderer pass strips active HTML, external links,
  remote references, and inline scripts before display/export.
- **Artifact ZIPs go through a declassification step.** Manifest generated; operator reviews and
  may exclude provenance/raw-data/log files; DLP hooks (regex-based scrubbing for API-key shapes,
  email addresses if configured) optionally apply.
- **Operator is the declassification authority.** Explicit in §4.
- **`.codex/*` artifacts in the job dir are excluded from default export.** The Codex backend
  writes `config.toml` and (currently) may also write `auth.json` in the job dir. These are
  **never** included in the export bundle by default in air-gap mode, regardless of what §12's
  scrubbing achieves — defense in depth.
- **Filesystem secret scan** runs over `.codex/config.toml`, `.codex/auth.json`, the artifact
  manifest, and the report before any export proceeds. Detected forbidden patterns (provider key
  shapes, `OPENSCIENTIST_SECRET_KEY` substring, DB URL substring) block the export.

---

## 12. Credential Minimization

Even with network egress blocked, secrets reachable in the agent environment can be embedded into
output and exported (§11). And the Codex backend writes secrets into job-visible config files
during normal operation. Air-gap mode strips both paths.

### 12.1 Strip list (agent container env)

In air-gap mode the agent container env contains only the secrets it strictly needs — which, with a
local LLM and local PubMed on the internal network, may be **zero cloud/GitHub/master secrets**.

**Strip:**

- All provider API keys: `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `AZURE_OPENAI_API_KEY`,
  `BEDROCK_*`, `GOOGLE_APPLICATION_CREDENTIALS` / Vertex SA paths, `ANTHROPIC_FOUNDRY_*` auth,
  `CBORG_*`
- `GITHUB_TOKEN`
- `OPENSCIENTIST_SECRET_KEY`
- Full `DATABASE_URL` (with credentials) — replace with job-scoped least-privilege creds
- Codex-specific: `CODEX_AUTH_HOST_PATH`, `CODEX_HOME` (handled separately via §12.2)
- Any service-account files

**Replace with:** a job-scoped DB credential, the internal-network hostnames/ports for LLM and
PubMed, and a minimal Codex MCP env (§12.2).

### 12.2 Codex MCP env + auth handling

The current `CodexAgent._mcp_env()` (`codex_agent.py:91-114`) copies the full parent env. In
air-gap mode this is replaced with explicit allowlist construction:

- A small whitelist of env vars the Codex CLI actually needs to operate (model selection, internal
  endpoint URL, MCP server URL — never credentials).
- The whitelist is hardcoded in `CodexAgent` air-gap mode; any env var outside it is dropped.
- `CODEX_HOME` is set to a per-job tmpfs path *outside* the job dir so the generated
  `config.toml` never lands in the exportable artifact tree.
- The Codex `auth.json` is **never copied into the job dir.** If Codex CLI needs credentials, they
  come from a host-side secret mounted read-only at `CODEX_HOME/auth.json` in a tmpfs, scoped to
  the subprocess lifetime. The `0644` / `0777` permissions Codex currently writes are explicitly
  rejected by the airgap container build (job dir permission verifier).

### 12.3 Startup verifier

A verifier scans the agent container's environment, `CODEX_HOME/config.toml` (if present), and the
job dir for `.codex/auth.json` / `.codex/config.toml`. If any forbidden secret pattern is present,
the job refuses to start.

The verifier list is the source of truth; the strip list (§12.1) is what the verifier checks for.

---

## 13. Control-Plane Egress in Air-Gap Mode

The agent can influence control-plane behavior via fields it writes (e.g. `job_meta`). Anything the
control plane then sends outbound is an indirect egress path. In air-gap mode the control plane
disables:

- ntfy notifications
- Pricing/cost fetches (`providers/pricing.py`)
- Skill sync from GitHub
- OAuth callbacks / external provider auth flows
- Any other outbound integration introduced later

A **fully offline deployment** additionally blocks the server container's outbound network at the
**host firewall**. Documented in `docs/AIR_GAPPED.md`; cannot be enforced from inside the
application. The §4 guarantee covers the agent runtime; the deployment hardening covers the rest.

---

## 14. Verification & Attestation

### `make airgap-verify` (CI gate)

Launches a real agent container under the full air-gap policy and runs probes:

**Negative probes (must ALL fail / time out):**

- DNS resolve of an external domain
- DNS resolve of a unique random subdomain (detects DNS-encoded exfil)
- TCP connect to public IPv4
- TCP/UDP/ICMP over IPv6 (if any v6 interface present)
- ICMP ping to public IPv4
- `python -c "import urllib.request; urllib.request.urlopen('https://example.com')"`
- `curl`/`wget` to external URL
- `pip install`, `uv pip install`, `cargo fetch`, `git ls-remote` to public remote
- `host.docker.internal` and the bridge gateway IP — unreachable
- Attempted Docker API ops from the agent — must fail at connect (no socket)
- **Codex CLI probes:** verify the generated Codex command/config has `network_access_enabled=False`
  and `web_search_enabled=False`; verify `config.toml` does not contain forbidden secrets;
  verify `auth.json` is not in the job dir
- **Claude SDK probes:** verify the SDK options have web/network built-ins disabled

**Positive probes (must succeed):**

- Reach the local LLM endpoint by IP:port (one for each backend's configured provider)
- Reach the local PubMed mirror

**Service-side probes (must ALL fail) — run from inside the LLM and PubMed containers:**

- Same negative probe set as above.

### Per-job signed attestation record

For every air-gap job, before the agent starts and at job end, the system produces a JSON
attestation containing:

- Master switch value + all derived invariants (§5)
- `docker network inspect` for the per-job network
- `ip route`, `ip -6 route` inside the agent container
- `nft list ruleset` (or `iptables-save -t filter`) for the per-job rules
- Resolver config (`/etc/resolv.conf` and `extra_hosts`)
- Image digests for agent, executor, LLM, PubMed, **Codex CLI binary**
- Engine version
- Probe transcripts (from a job-start mini run of `airgap-verify`)
- Credential-minimization verifier output (env env-allowlist, `.codex/config.toml` scan,
  `.codex/auth.json` absence)
- Provider-family `airgap_egress_targets()` result for the configured provider, with each target
  resolved to its allowlisted IP:port

The record is signed (job-scoped key) and stored alongside job artifacts.

---

## 15. Local PubMed (we own the path; we don't bundle the data)

OpenScientist provides the **tooling and documentation** to build a local PubMed without shipping
the corpus.

- `make download-pubmed` (long-running, resumable): pulls NCBI MEDLINE annual baseline + daily
  update files. Prints size/time/disk requirements; interruptible.
- **Corpus integrity:** hash manifest + snapshot ID for the loaded corpus. Snapshot ID recorded
  into each job's metadata.
- **Update mechanics:** documented offline process for daily updates, new snapshot IDs.
- **Local service:** thin shim exposing an eutils-compatible API (`esearch`/`efetch`) on the
  per-job internal network. Alternative (ES/Solr + adapter) discussed in the guide.
- **Service contract:** mirror satisfies §7 (no outbound, no remote fallback, bounded logs).
- **Repoint:** `PUBMED_BASE_URL` set to internal service; airgap validates internal resolution.

`docs/AIR_GAPPED_PUBMED.md` is the operator guide.

---

## 16. Configuration Surface

All new settings default to non-air-gapped behavior (G3). Likely additions:

| Setting (env) | Section | Purpose |
|---|---|---|
| `OPENSCIENTIST_AIR_GAPPED` | new / `ContainerSettings` | master switch (default `false`) |
| `OPENSCIENTIST_AIRGAP_LLM_ADDR` (Claude path) | `ProviderSettings` | IP:port of local LLM for Claude backend |
| `OPENSCIENTIST_AIRGAP_CODEX_LLM_ADDR` | `ProviderSettings` | IP:port of local OpenAI-compat endpoint for Codex backend |
| `OPENSCIENTIST_AIRGAP_PUBMED_ADDR` | new / `LiteratureSettings` | IP:port of local PubMed mirror |
| `PUBMED_BASE_URL` | new / `LiteratureSettings` | full URL the MCP tool targets |
| `OPENSCIENTIST_AIRGAP_SPARQL_ALLOW` | code-exec config | allowlist of internal SPARQL endpoints |
| `OPENSCIENTIST_AIRGAP_MIN_ENGINE` | new | minimum Docker Engine version (CVE-2024-29018) |
| `OPENSCIENTIST_AIRGAP_CODEX_BIN_SHA256` | new | pinned digest of bundled Codex CLI binary |
| `OPENSCIENTIST_AIRGAP_BEDROCK_ENDPOINT`, `_VERTEX_ENDPOINT` | new (optional) | explicit internal mappings for providers whose SDK doesn't expose endpoint override |

A single validated `air_gapped: bool` is what the rest of the code branches on.

---

## 17. Integration Points (file-level)

Refreshed against current `origin/main` + `feat/air-gapped` to reflect the provider refactor
(`c4f02df`) and the Codex backend addition (`39378aa`, `372034a`, `b5473d7`, `c282de9`,
`d47765c`, `7084179`).

### 17.1 Agent layer

- `src/openscientist/agent/factory.py:27-66` — `_instantiate_provider` + `get_agent`; backend
  dispatch via `ClaudeCompatible` / `CodexCompatible` markers from `providers/base.py:187-236`.
- `src/openscientist/agent/claude_code_agent.py:170-193` — Claude MCP + SDK `ClaudeAgentOptions` +
  `can_use_tool`. Air-gap: gate web/network SDK built-ins.
- `src/openscientist/agent/claude_code_agent.py:195-198` — provider env injection; minimize per §12.
- `src/openscientist/agent/codex_agent.py:91-114` — `_mcp_env`. Air-gap: replace with allowlist
  construction (§8.3, §12.2).
- `src/openscientist/agent/codex_agent.py:116-132` — `_write_codex_config`. Air-gap: write to a
  per-job tmpfs `CODEX_HOME`, not to `job_dir`; redact env before serialization.
- `src/openscientist/agent/codex_agent.py:166-192` — `ThreadOptions`. Air-gap: add
  `network_access_enabled=False` and `web_search_enabled=False`.

### 17.2 Providers

- `src/openscientist/providers/base.py:1-7, 52-236` — provider hierarchy + `ClaudeCompatible` /
  `CodexCompatible` marker mixins. **Add `airgap_egress_targets()` contract here** (§7.2).
- `src/openscientist/providers/__init__.py:14-54, 57-73` — registry + `_instantiate_provider`.
  Air-gap: invoke `airgap_egress_targets()` validation before returning the provider.
- Provider-specific endpoint construction (each must implement `airgap_egress_targets()`):
  - `providers/cborg.py:57-65` — `ANTHROPIC_BASE_URL` derived; deterministic.
  - `providers/foundry.py:192-223` — derives from `ANTHROPIC_FOUNDRY_RESOURCE` or override URL.
  - `providers/openai.py:60-75` — OpenAIDirect via Codex CLI's built-in OpenAI provider; default
    endpoint, may need override map.
  - `providers/azure_openai.py:58-84` — resource-scoped URL.
  - `providers/anthropic.py` — direct Anthropic; needs `ANTHROPIC_BASE_URL` for airgap.
  - `providers/bedrock.py:206-265`, `providers/vertex.py:209-284` — SDK regional clients;
    `airgap_egress_targets()` raises `AirGapUnsupportedError` unless explicit override mapping.

### 17.3 Containers & networking

- `src/openscientist/job_container/runner.py:54-77` — agent env build. Air-gap: apply credential
  strip per §12.1.
- `src/openscientist/job_container/runner.py:80-104` — job dir + Docker socket mount. Air-gap:
  **no socket** (§9); job dir permissions verifier rejects `0777`.
- `src/openscientist/job_container/runner.py:159-185` — Codex auth handling (`auth.json` to
  `job_dir/.codex/`). Air-gap: replace with tmpfs read-only secret mount (§12.2).
- `src/openscientist/job_container/runner.py:187-244` — Codex network resolve + container launch.
  Air-gap: per-job internal network + hardening flags (§6, §8).
- `src/openscientist/job_container/runner.py:227-244` — runtime hardening (current minimal).
  Air-gap: full hardening parity (§8).
- `src/openscientist/job_container/utils.py:resolve_docker_network` — bridge-fallback fatal in
  air-gap mode.
- `src/openscientist/container_manager.py:75-82` — `docker.from_env()`. (Note: top-level
  `container_manager.py`, *not* `job_container/container_manager.py` — v2 had this wrong.)
- `src/openscientist/container_manager.py:303-340` — executor network + container run. Air-gap:
  refactor callsite into control-plane executor-spawn service (§9, future work).
- `src/openscientist_tools/code_exec.py:129-161` — calls `ContainerManager.execute_code()`
  directly. Air-gap: replace with RPC to the control-plane spawn service (§9, future work).

### 17.4 Other

- `src/openscientist/literature.py:37` — hardcoded eutils `base_url`. Air-gap: thread through
  `PUBMED_BASE_URL` setting; no setting exists today.
- `src/openscientist/code_executor.py:27-57` — Python code-exec import allowlist. Air-gap: remove
  `requests` etc.
- `src/openscientist/code_executor.py:398-436` — SPARQL endpoint parse + call. Air-gap: add an
  allowlist; none exists today.
- `src/openscientist_tools/server.py:18-25` — MCP tool registration (unconditional). Air-gap:
  conditional gating extended from the `knowledge.py:146-150` hypothesis pattern to network tools.
- `src/openscientist/orchestrator/discovery.py` — report-generation phase hook.
- `src/openscientist/settings.py:35-143, 421-443` — `ProviderSettings`; `src/openscientist/settings.py:62-78, 117-122, 352-369` — OpenAI/Codex-specific settings. Air-gap settings (§16) live here.
- `Dockerfile.agent:14, 34-41` — base image + Codex CLI install. Air-gap: pre-bundled
  SHA256-pinned Codex binary; offline package manager config (§8.1).
- `Dockerfile.executor:4, 12-20, 36-38` — executor base + entrypoint.
- Skill ingestion path — disable GitHub fetch; pre-bundle + sign.
- Host firewall management (new): nftables/ip6tables rule application + teardown per job.
- Attestation writer (new): per-job signed JSON record.
- Output/export pipeline (new): report renderer hardening, artifact-pack declassification manifest,
  `.codex/*` exclusion (§11).
- Control plane: `ntfy.py`, `providers/pricing.py`, skill ingestion, OAuth flows — gated by
  air-gap mode (§13).
- `Makefile` — `download-pubmed`, `download-codex`, `airgap-verify` targets.

---

## 18. Phased Implementation Plan (after RFC approval)

**PR 1 — Foundation + the guarantee (everything required to make G1 honest):**

- `OPENSCIENTIST_AIR_GAPPED` master switch + fail-closed startup checks.
- `Provider.airgap_egress_targets()` contract on `providers/base.py` + per-provider implementations
  + startup validation in `providers/__init__.py`.
- **Executor-spawn service** in the control plane; **socket removed from agent containers** (§9).
- Per-job internal network, no multi-attach, no `host-gateway`/`extra_hosts`, IPv6 disabled,
  bridge-fallback fatal (§6).
- Host firewall management (nftables + ip6tables, per-job rules, attestation).
- Container hardening parity for agent + executor (§8).
- **Codex backend airgap path** (§8.1, §8.2, §8.3, §10.3, §12.2): `network_access_enabled=False`,
  `web_search_enabled=False`, MCP env allowlist construction, `CODEX_HOME` tmpfs, no `auth.json`
  in job dir, pre-bundled SHA256-pinned Codex binary.
- DNS hardening (§6.3).
- Cloud-provider rejection via `airgap_egress_targets()` (§7).
- **Credential minimization + verifier** including Codex paths (§12).
- Control-plane outbound disabled in mode (§13).
- Output / export boundary including `.codex/*` exclusion + filesystem secret scan (§11).
- `make airgap-verify` (negative + positive + service-side + Codex-config + Claude-options probes)
  and per-job signed attestation (§14). CI gate.

PR 1 is large because the guarantee can't be honestly claimed in parts.

**PR 2 — Local PubMed:** `PUBMED_BASE_URL` threading, `make download-pubmed`, eutils shim,
snapshot IDs, `docs/AIR_GAPPED_PUBMED.md`, mirror service contract.

**PR 3 — Defense-in-depth completions:** code-exec import tightening, SPARQL allowlist, MCP tool
gating, SDK + Codex built-in tool gating verification, MCP `cwd`/`.env` hardening, skills
pre-bundling + signing, package-manager offline mode.

**PR 4 — Operator deployment guide:** `docs/AIR_GAPPED.md` end-to-end, host-firewall recipe for
fully offline deployment.

---

## 19. Remaining Open Questions

1. **DNS architecture:** static `--add-host` vs. local non-recursive resolver. Both work.
2. **Provider explicit-mapping settings:** the shape of `OPENSCIENTIST_AIRGAP_BEDROCK_ENDPOINT` /
   `_VERTEX_ENDPOINT` / etc. — is a single URL enough or do these SDKs need a richer config?
3. **Codex `auth.json` provisioning:** the cleanest source for the read-only secret mount —
   operator-managed file, K8s secret, Docker secret? Will likely depend on deployment.
4. **Local PubMed service tech:** eutils shim vs ES/Solr + adapter.
5. **DLP scrubbing in export:** how aggressive a default?
6. **Engine version pin:** minimum patched Engine (CVE-2024-29018).
7. **Job-scoped DB credentials:** generation/lifecycle (Postgres roles vs. short-lived JWTs).

---

## 20. Residual Risk (honesty statement)

Air-gap mode prevents **unauthorized network connections** from agent and executor containers
(regardless of backend), verified by per-job attestation and the host firewall. It does **not**
address:

- The **report and artifact export channel** — operator-reviewed and operator-released; §11
  controls but ultimately operator responsibility.
- **What allowlisted local services do with the traffic they receive** — §7 service contracts.
- **Indirect channels** via operator interaction with poisoned outputs (link clicks etc.).
- **Host, kernel, Docker daemon compromise; malicious operator; physical access.**
- **Covert/side channels** (timing, cache, power, resource consumption).
- The **control-plane server's own outbound connections** without an additional host-firewall block
  (recommendation in §13).
- **Build-time supply chain** of agent/executor base images and the bundled Codex CLI binary
  beyond what SHA256 pinning catches.

The "guarantee" claim should always be cited with §4's precise statement and this section.

---

## 21. Revision Log

**v3 (2026-06-04) — after a third Codex review against current `origin/main`:**

- **Two agent backends in scope.** Threat model, design overview, §7, §8, §10, §12, §14, §17 all
  updated to handle the new `CodexAgent` (`codex` CLI subprocess) path alongside `ClaudeCodeAgent`,
  with per-backend enforcement clauses where the surface diverges (§1, §3, §4, §7.1, §10.2/10.3).
- **Provider-family endpoint contract.** Old §5 invariant #7 ("cloud providers refused unless
  endpoint resolves to internal LLM") was no longer implementable as a single check across seven
  providers with heterogeneous endpoint construction. Replaced with `Provider.airgap_egress_targets()`
  contract on `providers/base.py`, per-provider implementation, and startup validation in
  `providers/__init__.py`. Providers whose endpoint can't be made deterministic at startup
  (Bedrock/Vertex SDK regional clients) raise `AirGapUnsupportedError` and are refused unless
  explicitly mapped (§7.2).
- **Codex backend credential exfil.** `CodexAgent._mcp_env()` copies the parent env into
  `job_dir/.codex/config.toml`; host Codex auth can be copied to `job_dir/.codex/auth.json` with
  permissive permissions. New treatment: MCP env constructed from an allowlist (§8.3, §12.2);
  `CODEX_HOME` set to per-job tmpfs outside the job dir; `auth.json` never written to job dir, only
  mounted read-only from a tmpfs secret; verifier checks all three paths (§12.3).
- **Codex runtime + supply chain.** Requires `network_access_enabled=False` and
  `web_search_enabled=False` in `ThreadOptions` (§8.2); pre-bundled SHA256-pinned Codex CLI binary
  in the agent image, no networked build-time install (§8.1).
- **§11 Export Boundary** extended to exclude `.codex/*` from default export and filesystem-scan
  for secrets in `.codex/config.toml` and `.codex/auth.json` before any export proceeds.
- **§4 Threat Model** vector list expanded to call out the Codex LLM call, Codex web search, the
  Codex config and auth files, and Codex binary supply chain.
- **§17 Integration Points** refreshed against current `origin/main`. Notable corrections:
  - `BaseProvider` removed; markers `ClaudeCompatible` / `CodexCompatible` in `providers/base.py:187-236`.
  - `providers/base_v2.py` does not exist; `base.py` is the file.
  - `container_manager.py` is top-level (`src/openscientist/container_manager.py`), not under
    `job_container/`. `:75-82` is the `docker.from_env()` callsite.
  - `code_exec.py` still calls `ContainerManager.execute_code()` directly at `:129-161`; the RFC's
    executor-spawn service is **future work** (called out honestly in §9).
  - `runner.py` line ranges updated to current layout (`:54-77` env build, `:80-104` socket/mount,
    `:159-185` Codex auth, `:187-244` network + launch, `:227-244` hardening).
  - `code_executor.py:27-57` for imports, `:398-436` for SPARQL.
  - `openscientist_tools/server.py:18-25` imports tools unconditionally; only `knowledge.py:146-150`
    conditionally registers.
  - `literature.py:37` is a hardcoded base URL, not a setting yet.
- **§18 Phased Plan** updated to include Codex-specific tasks in PR-1 explicitly.

**v2 (2026-05-29) — after two adversarial Codex reviews:** see prior revision in git history
(commit before `a2c6a65`).
