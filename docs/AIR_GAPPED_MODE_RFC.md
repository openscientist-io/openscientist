# RFC: Air-Gapped / Zero-Egress Mode

**Status:** Draft v4 — for review (no implementation merged yet, but PR-1 scaffold is underway). Revised after four adversarial Codex reviews; see §21 Revision Log.
**Date:** 2026-06-05
**Author:** Justin Reese (with Claude; reviews by OpenAI Codex)
**Related:** [`DESIGN.md`](DESIGN.md), [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md), [`DEPLOYMENT.md`](DEPLOYMENT.md)

---

## 1. Motivation

OpenScientist is increasingly pointed at sensitive data (patient phenopackets, unpublished
clinical/-omics datasets). Operators in regulated or classified environments need a defensible
guarantee that **no unauthorized network connections occur from the agent runtime** — not to
Anthropic / OpenAI / Azure / NCBI, not from agent-authored Python, not via `curl`/`wget`/DNS tricks,
and not via the interactive post-job chat surface either.

This RFC proposes an opt-in **air-gapped mode**. Default deployment unchanged. A tech-savvy
operator who follows the documented setup gets a deployment where unauthorized egress is
**physically prevented at the network layer** and **demonstrably so** (per-job signed attestation).

The guarantee is precise (§4). **Network-connectivity guarantee, not "nothing leaves the box"** —
the report and artifacts are designed export channels and are addressed separately (§11).

> **Two agent backends are in scope.** OS now ships `ClaudeCodeAgent` (Claude Code SDK as a
> subprocess) and `CodexAgent` (OpenAI `codex` CLI as a subprocess) with a two-axis hierarchy:
> each `Provider` declares which backend family it's compatible with (`ClaudeCompatible` /
> `CodexCompatible`), and `agent/factory.py` dispatches accordingly. Both backends spawn their own
> subprocess inside the agent container and each carries its own config/auth surface. The
> network-layer guarantee (§6) holds uniformly; the configuration-level enforcement diverges and
> is called out per-backend where it does.

> **The Codex Review-3 diff-minimization finding shapes the implementation.** Rather than
> modifying `providers/base.py`, every provider subclass, and the `CodexAgent` class to add
> air-gap behavior, this RFC keeps all air-gap-specific logic in a new
> `src/openscientist/airgap/` package that *reads* provider/agent metadata rather than *modifying*
> their contracts. PR-1 modifies four of Luca's recently-touched files (`settings.py`,
> `agent/factory.py`, `job_container/runner.py`, `container_manager.py`) for a total of ~60
> conditional lines and adds ~7 new files in `airgap/`. See §18 for the full file list.

---

## 2. Goals and Non-Goals

### Goals

- **G1 — Bounded, defensible guarantee.** No unauthorized network connections from agent or
  executor containers (regardless of backend) to anything outside an IP:port-allowlisted set of
  local services. Enforced by the kernel and host firewall, not by application code.
- **G2 — Attested, not asserted.** Per-job signed attestation record (network inspect, routes,
  resolver config, firewall snapshot, probe transcripts, image digests, Codex CLI binary digest,
  generated `config.toml` scan result) stored alongside job artifacts; CI runs `make airgap-verify`
  as a regression gate.
- **G3 — Default-off.** Normal OpenScientist behavior is byte-for-byte unchanged when the mode is
  disabled.
- **G4 — Fail-closed.** If any invariant can't be established (network can't be made internal,
  host firewall not applied, required local service unreachable, IPv6 not disabled, cloud provider
  credentials present, Codex CLI network access not disabled), jobs or the server refuse to start
  with explicit errors.
- **G5 — Offline literature.** Documented path to run PubMed search against a local mirror with
  corpus integrity (snapshot IDs, hash manifests).
- **G6 — Output channel discipline.** Report and artifact bundle are explicit, audited *export*
  channels with declassification review (§11). The `.codex/` config and auth artifacts the Codex
  backend writes are subject to the same review.
- **G7 — Operator experience.** Makefile targets + thorough docs make a correct air-gapped install
  achievable without guesswork.
- **G8 — Mergeable.** PR-1 minimizes its footprint on the agent/provider code Luca refactored
  recently. All air-gap-specific logic lives in `src/openscientist/airgap/`; the core code gets
  small conditional branches in 4 files, no per-provider subclass changes.

### Non-Goals

- **Not** defending against a malicious operator, host compromise, or Docker daemon compromise.
- **Not** eliminating covert/side channels (timing, resource, power).
- **Not** building the local LLM serving stack — the operator stands up
  Anthropic-compatible or OpenAI-compatible endpoints on the internal network and points OS at
  them.
- **Not** treating the report/artifacts as a covered network channel — operator-reviewed export.

### Stance on the control plane

A previous draft listed the control-plane web server's own outbound connections as out of scope.
Codex pushed back: the agent can write fields the control plane then ships out (ntfy notifications,
pricing fetches, OAuth callbacks, skill sync). Air-gap mode therefore also disables the control
plane's outbound integrations when the master switch is on (§13). A fully offline *deployment*
additionally requires the operator to block the server container's network at the host firewall —
a deployment recommendation, not a code-level guarantee.

### Stance on the post-job chat surface

A new gap from the Review-3 pass: `src/openscientist/job_chat.py` instantiates a `ClaudeCodeAgent`
directly inside the orchestrator process (not in a container) to support interactive post-job chat
against the agent's findings. This is **outside the container boundary entirely** and §4's
network-layer guarantee can't cover it as written. Air-gap mode addresses this by either:

1. Disabling the post-job chat surface when `airgap_mode=True` (simplest, recommended for PR-1), or
2. Running the chat agent in a per-session ephemeral container subject to the same network policy
   as the job's own agent container (cleaner, deferred to a follow-up PR).

Settled for v4: **PR-1 disables `job_chat.py` interactive sessions in airgap mode** with a clear
"interactive chat is disabled in air-gapped deployments; export the report instead" message. The
in-container path is a v2 improvement.

---

## 3. The Core Insight

You cannot get a guarantee by enumerating and disabling tools (`wget`, `lynx`, `requests`,
SPARQL, …). The guarantee comes from the network layer: agent/executor containers have *no route
off the box* — verified by the kernel, the routing table, and the host firewall.

Network-layer isolation alone is **necessary, not sufficient**. Each agent backend has its own
subprocess with its own config/auth surface. Output channels (report, artifact ZIP, `.codex/*`
files) leave the box by design and need their own discipline. The agent's `exec()`-driven Python
in `code_executor.py` can `import socket` directly, bypassing any `requests`-wrapper allowlist
— meaning code-execution containers need kernel-level network namespace control, not just an
import filter.

The design is **layered**: kernel routing (internal network) + host firewall (default-DROP,
IP:port allowlist) + Docker configuration (per-job network, no multi-attach, no host-gateway,
IPv6 off) + executor container network namespace (no egress at all, even to the allowlisted LLM)
+ per-backend application-layer enforcement (gated SDK built-ins, Codex `network_access_enabled`
+ `web_search_enabled` off) + credential minimization (active-provider-only allowlist) + output
discipline + disabled interactive chat surface.

---

## 4. Threat Model

### Adversary

The **untrusted party is the agent itself** — an LLM (driven by either backend) that may be
steered into exfiltration by **prompt injection** in the material it processes. We assume the
agent may attempt, with full creativity, to:

(a) open network connections to external hosts via any subprocess it controls;
(b) coerce allowlisted local services into relaying;
(c) encode data into outputs the operator later exports;
(d) write files that influence later runs;
(e) spawn sibling containers to bypass its own isolation;
(f) leverage its backend's subprocess (Claude Code SDK or `codex` CLI) — including the
config/auth files those subprocesses read and the built-in tools they expose (Codex's web search,
Claude's web fetch) — to reach the network;
(g) import network modules (`socket`, `urllib`, `http.client`) directly inside `exec()`'d Python
in the executor container, bypassing any `requests`-wrapper allowlist;
(h) embed exfiltrated content in fields the post-job interactive chat (`job_chat.py`) renders
back to a user who clicks links or follows references.

The operator, host OS, kernel, and Docker daemon are **trusted**. The operator is the
**declassification authority for outputs** (§11).

### In-scope egress vectors (must all be closed)

| Vector | Today | Status in v4 |
|---|---|---|
| Claude SDK LLM call | direct to Anthropic/Foundry/etc. | rejected at startup unless internal endpoint (§7) |
| **Codex CLI LLM call** | direct to OpenAI / Azure OpenAI | rejected at startup unless internal endpoint (§7); `network_access_enabled=False` (§8) |
| **Codex CLI built-in web search** | enabled by default | `web_search_enabled=False` required in airgap (§10) |
| PubMed search | hardcoded NCBI eutils URL | repointed to internal mirror (§15) |
| **Agent Python `import socket` / `urllib.request` in `exec()`** | unconstrained | executor container network namespace **has no egress at all** (not even to the LLM endpoint); kernel-level (§10.2) |
| SPARQL `# ENDPOINT:` queries | arbitrary | allowlisted to internal endpoints only (§10) |
| Shell tools | unrestricted | network unreachable; offline-mode env defaults (§10) |
| DNS exfiltration | resolver reachable & forwarding | no external resolver; static hosts or local non-recursive resolver; unique-subdomain probes (§6.3) |
| Sibling containers via Docker socket | RW-mounted | airgap-only Docker socket proxy with hard-coded default-deny policy (§9; intermediate before the full extraction in a later PR) |
| Skill ingestion from GitHub | active | disabled in mode; skills pre-bundled, signed (§10) |
| Multi-network attachment, `host-gateway`, `extra_hosts` | possible | forbidden (§6.2) |
| IPv6 egress | possible | disabled per network/container + ip6tables DROP (§6.4) |
| NET_RAW / packet sockets | default cap | `cap_drop=ALL` (§8) |
| Control-plane outbound | active | disabled in mode (§13) |
| MCP `cwd=job_dir` `.env` discovery | possible | disabled (§10) |
| **`get_agent_env()` passes ALL provider credentials** to every job regardless of which provider is active | a job using Anthropic still receives `OPENAI_API_KEY`, Foundry token, Bedrock keys, etc. | **active-provider-only credential allowlist** (§12.1) |
| **Codex CLI config (`job_dir/.codex/config.toml`)** | written from copy of parent env (DB URL, master secret, provider creds) | constructed from a minimal allowlist (§12.2) |
| **Codex CLI auth (`job_dir/.codex/auth.json`)** | may land in job dir with `0644` in a `0777` parent | never written to job dir; tmpfs read-only secret mount (§12.2) |
| **Codex CLI binary supply chain** | downloaded from GitHub release at image-build time, no checksum | SHA256-pinned, pre-bundled in agent image (§8.1) |
| **Post-job interactive chat (`job_chat.py`)** | instantiates `ClaudeCodeAgent` in orchestrator process, has filesystem access to job artifacts, outside container boundary | disabled in airgap mode in PR-1; in-container chat is a v2 improvement (§2 stance) |

### Out-of-scope (covered separately)

- **Output channel** (report, artifacts, `.codex/*` files): operator-reviewed export → §11.
- **Allowlisted local services as sinks**: §7 service contracts.
- **Credentials reachable in env**: §12 credential minimization.

### Out of scope entirely

- Host or kernel or Docker daemon compromise; malicious operator; covert/side channels.

### Precise guarantee (cite with §11 and §20)

> When air-gapped mode is enabled, all invariants (§5) are established at job start, the per-job
> attestation passes (§14), and the host's firewall policy is in effect, **no process inside an
> agent container, executor container, or any container spawned for that job — regardless of which
> agent backend (`ClaudeCodeAgent` or `CodexAgent`) the job uses — can open a network connection to
> any host other than the explicitly allowlisted local services (by IP and port), as enforced by
> Docker network configuration, host nftables/ip6tables, and the kernel's routing tables. The
> executor containers used for `exec()`-driven code execution have no egress route at all (not
> even to the allowlisted LLM endpoint); their network namespace is fully isolated.** Restricted
> to network-layer connectivity from container-resident processes; does not cover
> operator-reviewed export of report/artifact files (§11), what an allowlisted local service does
> with traffic it receives (§7 contracts), the orchestrator-process post-job chat (disabled in
> airgap mode per §2), or side channels.

---

## 5. Design Overview

A single master switch — **`OPENSCIENTIST_AIR_GAPPED`** (default `false`) — is the source of
truth. When `true`, it deterministically establishes all of the following invariants; **none is
optional**. Inability to establish any one of them is fail-closed startup error (G4):

1. **Per-job internal Docker network** with no gateway/NAT (§6).
2. **Host firewall** (nftables + ip6tables) default-DROP from the per-job bridge, allowlist only
   the local LLM and PubMed services by **IP:port** (§6).
3. **IPv6 disabled**; `host-gateway`/`extra_hosts`/multi-network forbidden; single network per
   container (§6.2).
4. **DNS hardened**: no external recursive resolver (§6.3).
5. **Docker socket access restricted** — airgap-only socket proxy with hard-coded default-deny
   policy: only `create`/`start`/`wait`/`logs`/`remove` for executor-labeled containers,
   network/mounts/privilege/exec/inspect all denied (§9).
6. **Container hardening parity** — agent and executor identically hardened (caps, seccomp,
   AppArmor/SELinux, userns, no-new-privileges, read-only rootfs, pids limit, digest pinning) (§8).
7. **Executor container has no egress route at all.** Its network namespace is fully isolated
   from the per-job bridge; not even the LLM endpoint is reachable. Code execution doesn't need
   network. This kills the `import socket`/`urllib.request` bypass of any `requests` allowlist
   (§10.2).
8. **Provider endpoint validation via external registry.** `airgap/egress_registry.py` maps
   `provider_id → set[(host, port)]` deterministic egress targets per provider; air-gap startup
   walks the configured provider's egress set and refuses to run if any target doesn't resolve to
   the allowlisted internal LLM. Providers whose endpoint can't be made deterministic at startup
   (Bedrock/Vertex SDK regional clients without override) raise `AirGapUnsupportedError` and are
   refused unless explicitly mapped (§7). **The registry is external to `providers/base.py` and
   every provider subclass; nothing in those files changes.**
9. **Codex backend reconfigured via `AirgapCodexAgent` subclass.** `agent/factory.py` selects
   `AirgapCodexAgent(CodexAgent)` from `airgap/codex_agent.py` when both `airgap_mode=True` and
   the provider is `CodexCompatible`. The subclass overrides 2–3 helper methods to:
   - construct the MCP env from an allowlist (not `os.environ.copy()`);
   - write the Codex `config.toml` to a per-job tmpfs `CODEX_HOME` outside the job dir;
   - set `network_access_enabled=False` and `web_search_enabled=False` on the `ThreadOptions`.
   `agent/codex_agent.py` itself gets at most ~20 lines of refactor to make those helpers
   overridable; **its public contract is unchanged**.
10. **PubMed repointed** to internal mirror (§15).
11. **Defense-in-depth tool/code gating** (§10): network-touching MCP and SDK built-in tools
    (Claude *and* Codex) disabled, `.env` discovery disabled, SPARQL allowlist, `requests` import
    removed from code-exec allowlist (defense in depth on top of the kernel namespace).
12. **Skills pre-bundled, signed, frozen** for the job; GitHub fetch disabled (§10).
13. **Active-provider-only credential allowlist.** `settings.get_agent_env(active_provider_id)`
    strips all provider credentials except those needed by the active provider, plus
    `GITHUB_TOKEN`, master secret, full DB URL. The verifier scans the resulting env,
    `CODEX_HOME/config.toml`, and the job dir for forbidden secret patterns and refuses to start
    if any are present (§12).
14. **Control-plane outbound disabled** (ntfy, pricing, skill sync, OAuth) (§13).
15. **Post-job interactive chat (`job_chat.py`) disabled** with a clear operator-facing message
    (§2 stance).
16. **Export boundary active**: report/artifact ZIP pass through declassification review;
    `.codex/*` artifacts excluded from default export; filesystem secret scan over
    `.codex/config.toml` + `.codex/auth.json` + report + manifest before any export proceeds
    (§11).
17. **Per-job signed attestation** produced before and after the agent run (§14).

---

## 6. Network Isolation — Layered

### 6.1 Per-job internal Docker network

For each job, create a fresh network with `internal: true` and `enable_ipv6: false`. Per-job, not
shared, for blast-radius reduction. Auto-detection fallback to `bridge` in
`job_container/utils.py:resolve_docker_network` is **fatal in air-gap mode**. `network_mode` of
`host`, `container:<id>`, `macvlan`, `ipvlan`, `network connect` during the job are **forbidden**.

### 6.2 Host firewall

`internal: true` is necessary but not sufficient. Air-gap mode installs **host-level nftables (and
ip6tables) rules with default-DROP** on the per-job bridge, allowing only IP:port flows to the
local LLM and PubMed services. These rules are part of the attestation evidence (§14).

- Forbid `host-gateway` and `extra_hosts`.
- Single network attachment per container.
- Drop `NET_RAW`.
- Require patched Docker Engine (CVE-2024-29018 leaked DNS externally on certain Moby versions).

### 6.3 DNS handling

No external recursive resolver. Either no DNS + `--add-host` for the allowlist, or a small local
non-recursive resolver bound to the per-job network that answers only the allowlist. Embedded
Docker DNS (127.0.0.11) configured to never forward upstream. Unique-subdomain probe in
attestation to verify no DNS-encoded exfil reaches external.

### 6.4 IPv6

Disabled per network and per container. If a deployment requires v6, ip6tables default-DROP +
explicit allow + IPv6 attestation probes. Default off.

### 6.5 Evidence

Per-job attestation captures `docker network inspect`, `ip route`, `ip -6 route`, and
`nft list ruleset` (or `iptables-save`).

---

## 7. Allowlisted Local Services + Service Contracts

Two services live on each per-job internal network and are the **only** hosts the agent's
container can reach:

1. **Local LLM endpoint(s)** — operator-stood-up; one or more depending on which backend(s) the
   deployment runs.
2. **Local PubMed mirror** (§15).

### 7.1 The two LLM-call paths

Both backends consume from the same allowlist via different subprocesses:

| | `ClaudeCodeAgent` | `CodexAgent` |
|---|---|---|
| Subprocess | Claude Code SDK | OpenAI `codex` CLI |
| Provider families | `ClaudeCompatible` (Anthropic, Bedrock, Vertex, Foundry, CBORG) | `CodexCompatible` (OpenAIDirect, Azure OpenAI) |
| Endpoint config | per-provider env (`ANTHROPIC_BASE_URL`, etc.) | Codex `config.toml` (generated by the agent) |
| Network controls | gated SDK built-in tools | `ThreadOptions.network_access_enabled` + `web_search_enabled` |
| Auth surface | env-injected provider creds | `auth.json` mounted as tmpfs read-only secret |

The kernel-level network boundary (§6) constrains both equally. The configuration-level
enforcement diverges and is parallel.

### 7.2 External egress registry (replaces the in-class contract)

In v3 the design called for a `Provider.airgap_egress_targets()` method on `providers/base.py`
implemented by every provider subclass. Codex Review-3 flagged this as unnecessary churn against
Luca's recently-rewritten provider code: the two-axis hierarchy (`ClaudeCompatible` /
`CodexCompatible` markers) is already enumerable, and the air-gap-specific logic can read provider
metadata externally.

PR-1 ships the equivalent as **`src/openscientist/airgap/egress_registry.py`**: a dispatch table
keyed on `provider_id` that returns the deterministic IP:port set the provider would talk to in
air-gap configuration.

```python
# airgap/egress_registry.py (sketch)
EGRESS_TARGETS: dict[str, Callable[[Settings], set[tuple[str, int]]]] = {
    "anthropic":    lambda s: _from_url(s.provider.anthropic_base_url) if s.provider.anthropic_base_url else _unsupported(),
    "cborg":        lambda s: _from_url(s.provider.anthropic_base_url),
    "foundry":      lambda s: _from_foundry_resource(s.provider.anthropic_foundry_resource),
    "openai":       lambda s: _from_url(s.provider.openai_base_url) if s.provider.openai_base_url else _unsupported(),
    "azure_openai": lambda s: _from_azure_resource(s.provider.azure_openai_resource),
    "bedrock":      lambda s: _unsupported("Bedrock SDK regional client — set OPENSCIENTIST_AIRGAP_BEDROCK_ENDPOINT explicitly"),
    "vertex":       lambda s: _unsupported("Vertex SDK regional client — set OPENSCIENTIST_AIRGAP_VERTEX_ENDPOINT explicitly"),
}

def validate_provider_for_airgap(provider_id: str, settings: Settings, allowlist: set[tuple[str, int]]) -> None:
    targets = EGRESS_TARGETS[provider_id](settings)
    extra = targets - allowlist
    if extra:
        raise AirGapPolicyError(f"Provider {provider_id} would reach {extra}, not in allowlist")
```

A `pytest` fixture (`tests/airgap/test_egress_registry.py`) asserts `set(EGRESS_TARGETS.keys())`
equals the set of registered `provider_id`s from `providers/__init__.py`, so adding a new provider
without adding a registry entry fails CI rather than silently bypassing the check.

**Nothing in `providers/base.py` or any provider subclass changes.**

### 7.3 Service contracts

Each allowlisted service must satisfy:

- **No outbound network.** Service container on the per-job internal network only; nftables/
  ip6tables block egress from it; attested.
- **No remote fallback.** Local mirror must not call NCBI on cache miss; local LLM must not call
  cloud.
- **Bounded logs.** Size-bounded, rotated, reviewable.
- **No write-back to job dir.**
- **Service-side attestation.** `airgap-verify` runs negative egress probes from *inside* the
  service containers as well as the agent's.

---

## 8. Container Hardening (parity: agent and executor)

Agent and executor containers hardened identically. Air-gap mode enforces on both:

| Control | Notes |
|---|---|
| Per-job internal network (§6.1) — except **executor has no egress route at all** (§5 invariant #7) | kernel boundary |
| Host firewall default-DROP + IP:port allowlist (§6.2) | real enforcement |
| Docker socket access via airgap proxy only | §9 |
| `cap_drop=["ALL"]`, minimal explicit `cap_add` | drops `NET_RAW` |
| Custom seccomp profile | minimal syscalls |
| AppArmor or SELinux profile | mandatory |
| `no-new-privileges` | already present |
| User-namespace remap | host-uid isolation |
| Non-root user | already present |
| `read_only=True` rootfs + explicit tmpfs | tampering / persistence |
| `pids_limit`, file-size, process/wall-clock timeouts | runaway containment |
| Digest-pinned base images | reproducible |
| `dns` set per §6.3 | no upstream resolver |
| `extra_hosts: []`, single network attachment | §6.2 invariants |

### 8.1 Codex CLI binary supply chain

The agent image currently installs `openai-codex-sdk` from PyPI and downloads the Codex CLI binary
from GitHub release URLs at image-build time. Air-gap mode requires:

- Pre-bundled Codex CLI binary at SHA256-pinned digest in the agent image.
- No networked install of `openai-codex-sdk` or the Codex CLI at build time.
- Codex CLI binary digest captured in per-job attestation (§14).
- `make download-codex` documents the trusted-download + checksum step.

### 8.2 Codex backend runtime hardening (via subclass)

The Codex backend currently sets `sandbox_mode="danger-full-access"` and `approval_policy="never"`
but does *not* set `network_access_enabled=False` or `web_search_enabled=False`. Air-gap mode adds
these via the subclass:

```python
# airgap/codex_agent.py (sketch)
class AirgapCodexAgent(CodexAgent):
    """CodexAgent with air-gap policy applied.
    
    Overrides the helper methods that build the MCP env, write config.toml,
    and assemble ThreadOptions, leaving the public contract identical.
    """

    def _build_mcp_env(self) -> dict[str, str]:
        # Allowlist-construct instead of os.environ.copy()
        return env_allowlist.codex_mcp_env(self._settings, self._active_provider_id)

    def _codex_home(self) -> Path:
        # Per-job tmpfs, outside the job_dir export tree
        return Path("/run/codex-home") / self._job_id

    def _thread_options(self) -> ThreadOptions:
        opts = super()._thread_options()
        return opts.replace(network_access_enabled=False, web_search_enabled=False)
```

`agent/codex_agent.py` gets ~20 lines of refactor to expose `_build_mcp_env`, `_codex_home`, and
`_thread_options` as overridable helpers, but its public method signatures don't change.

`agent/factory.py` adds one conditional (~3 lines): if `settings.airgap_mode and isinstance(provider, CodexCompatible)`, instantiate `AirgapCodexAgent` instead of `CodexAgent`.

### 8.3 Verification

The per-job attestation (§14) inspects the generated Codex CLI command / config to verify both
flags are present, and scans the `config.toml` for forbidden secret patterns.

---

## 9. Executor Spawn Architecture (revisited: airgap-only socket proxy)

**Open question revisited.** The v2/v3 RFC settled on "Option A: remove the Docker socket from
agent containers; spawn executor containers from a trusted control-plane component." That's still
the right end state, but it's a substantial refactor of `container_manager.py:303-340` and
`openscientist_tools/code_exec.py:129-161` that would also affect non-airgap deployments.

Codex Review-3 noted that **the previous calculus dismissing Option B (Docker socket proxy)
changes when the proxy is only active in airgap mode**. A misconfigured proxy in the general path
would break all container operations; an airgap-only proxy's blast radius is limited to airgap
deployments, and the direct socket path is unchanged for everyone else.

**PR-1 ships the airgap-only socket proxy as an intermediate step.** A separate follow-up PR (PR-2
or later) does the full extraction to a control-plane spawn service.

The proxy:

- Selected via a `socket_path` parameter in `container_manager.py` that switches between
  `/var/run/docker.sock` (default) and the airgap proxy socket (when `airgap_mode=True`).
- Implemented as a thin layer in `airgap/docker_proxy.py` — either wrapping
  `tecnativa/docker-socket-proxy` (image-based) or a custom `socat`+filter wrapper. PR-1 picks one
  in the implementation; the design is identical.
- **Hard-coded default-deny.** Allowlist: `POST /containers/create` (with body-shape validation
  pinning network to the per-job internal network), `POST /containers/{id}/start`,
  `GET /containers/{id}/json`, `GET /containers/{id}/logs`, `POST /containers/{id}/wait`,
  `DELETE /containers/{id}`. Denylist: everything else (in particular `exec`/`cp`/`inspect` on
  unrelated containers, `network connect`, `network create`, image pull/build/load/import, volume
  ops, plugins, container update, host PID/IPC/UTS, bind mounts, `extra_hosts`, `--privileged`,
  `--cap-add`, `--device`).

This delivers the §4 security benefit (sibling-container egress vector closed) without the
structural refactor. `container_manager.py` changes are ~10 conditional lines.

---

## 10. Defense-in-Depth (application layer)

Not the boundary (§3) — these make failures fast and legible.

### 10.1 Backend-agnostic

- **PubMed:** `literature.py:37` `base_url` becomes `PUBMED_BASE_URL`; in air-gap mode it must be
  internal or the tool is disabled (§15).
- **MCP tool gating:** extend the conditional-registration pattern (currently only hypothesis
  tools at `openscientist_tools/knowledge.py:146-150`) to all network-touching tools in air-gap
  mode. `airgap/mcp_filter.py` returns the airgap-filtered tool list; `agent/factory.py` passes it
  to whichever agent backend the factory selected.
- **Package managers:** `pip`, `uv`, `cargo`, `git` configured offline by default in agent /
  executor images. Attestation probes them.
- **Skills:** pre-bundled, signed, immutable for the duration of the job; GitHub ingestion path
  disabled.
- **MCP `cwd=job_dir` `.env`:** discovery disabled in agent/MCP/executor contexts.

### 10.2 Code execution: kernel namespace, not just import allowlist

Codex Review-3 flagged that `src/openscientist_tools/code_exec.py:180-200` uses `exec(code,
namespace)` to give the agent direct Python — which can `import socket` / `urllib.request` /
`http.client` directly, bypassing any `requests`-wrapper allowlist. The v2/v3 wrapper-only fix was
trivially defeatable.

Air-gap mode addresses this at the network layer of the **executor** container specifically:

- The executor container that runs `exec()`'d Python has its **own** network namespace fully
  isolated — no bridge to the per-job network, no DNS, no route. Not even the LLM endpoint is
  reachable from inside `exec()`'d code.
- Code execution **doesn't need network**. The agent's network-requiring tools (`search_pubmed`,
  `validate_citation`, the LLM call itself) run in the agent container, not the executor; they're
  gated by the airgap allowlist (§7). The executor only runs the agent's Python; that Python has
  no legitimate reason to phone home.
- The import-allowlist (drop `requests` etc. from the code-exec allowlist) remains as fail-fast
  UX, but is no longer the security boundary.

### 10.3 Claude Code SDK backend

- **SDK built-in tool gating.** Claude Code's SDK ships with its own tools (web fetch, etc.)
  separate from MCP-registered ones. Air-gap mode disables web/network-capable built-ins via
  `agent/claude_code_agent.py:170-193`. Implementation: `airgap/mcp_filter.py` returns the
  filtered set of allowed built-ins; `claude_code_agent.py` gets a one-line conditional reading
  from `mcp_filter.allowed_claude_builtins(settings)`.

### 10.4 Codex CLI backend

Per §8.2 via the subclass: `network_access_enabled=False`, `web_search_enabled=False`, MCP env
allowlist, `CODEX_HOME` tmpfs, no `auth.json` in job dir.

### 10.5 SPARQL

Validate `# ENDPOINT:` against an allowlist (`code_executor.py:398-436`). Add via
`airgap/sparql_allowlist.py` referenced by `code_executor.py` only when `airgap_mode=True`.

---

## 11. Output / Export Boundary

The job's **report** and **artifact ZIP** are designed bulk export channels. They leave the box
when the operator downloads them.

In air-gap mode:

- **Reports treated as untrusted output.** Renderer strips active HTML, external links, remote
  references, inline scripts.
- **Artifact ZIPs go through declassification.** Manifest generated; operator reviews; DLP
  scrubbing (regex-based for API-key shapes, etc.) optional.
- **Operator is the declassification authority.**
- **`.codex/*` artifacts excluded from default export.** Codex backend writes `config.toml` and
  (without the airgap subclass) may write `auth.json` in the job dir. Air-gap mode excludes the
  whole `.codex/` subtree from artifact ZIPs, regardless of the §12 stripping efforts — defense
  in depth.
- **Filesystem secret scan** runs over `.codex/config.toml`, `.codex/auth.json`, the artifact
  manifest, and the report. Detected forbidden patterns block export. Implemented in
  `airgap/export_boundary.py`.

---

## 12. Credential Minimization

### 12.1 Active-provider-only credential allowlist

Codex Review-3's new finding: `src/openscientist/settings.py:get_agent_env()` collects **all**
configured provider credentials and passes them to every job container regardless of the active
provider. An air-gap job using Anthropic still receives `OPENAI_API_KEY`, Foundry token, Bedrock
keys, etc. — every one a potential exfil channel if any allowlisted local service ever logs
inbound credentials by mistake.

Air-gap mode requires:

- `settings.get_agent_env()` takes an `active_provider_id` parameter (or reads it from settings).
- **Only** credentials for the active provider are included in the agent env.
- All other provider credentials are stripped.
- Plus `GITHUB_TOKEN`, `OPENSCIENTIST_SECRET_KEY`, full `DATABASE_URL` (with credentials) all
  stripped; the agent gets a job-scoped least-privilege DB credential instead.
- Codex-specific paths handled per §12.2.

The implementation lives in `airgap/env_allowlist.py`:

```python
# airgap/env_allowlist.py (sketch)
PROVIDER_ENV_VARS = {
    "anthropic":    {"ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL"},
    "openai":       {"OPENAI_API_KEY", "OPENAI_BASE_URL"},
    "azure_openai": {"AZURE_OPENAI_API_KEY", "AZURE_OPENAI_RESOURCE"},
    "foundry":      {"ANTHROPIC_FOUNDRY_RESOURCE", "ANTHROPIC_FOUNDRY_AUTH_TOKEN"},
    # ...
}

def filtered_agent_env(full_env: dict, active_provider_id: str) -> dict:
    allowed = (BASE_AIRGAP_ENV
               | PROVIDER_ENV_VARS.get(active_provider_id, set())
               | {"OPENSCIENTIST_AIRGAP_LLM_ADDR", "OPENSCIENTIST_AIRGAP_PUBMED_ADDR"})
    return {k: v for k, v in full_env.items() if k in allowed}
```

`settings.get_agent_env()` gets a one-line conditional: if `airgap_mode`, route through
`env_allowlist.filtered_agent_env(env, active_provider_id)`.

### 12.2 Codex MCP env + auth handling

The current `CodexAgent._mcp_env()` copies the full parent env. The `AirgapCodexAgent` subclass
(§8.2) overrides `_build_mcp_env()` to call `env_allowlist.codex_mcp_env(...)`:

- A small whitelist of env vars Codex actually needs (model selection, internal endpoint URL,
  MCP server URL — never credentials).
- `CODEX_HOME` set to a per-job tmpfs path **outside the job dir** so the generated `config.toml`
  doesn't land in the exportable artifact tree.
- Codex `auth.json` **never copied into the job dir.** Credentials come from a host-side secret
  mounted read-only at `CODEX_HOME/auth.json` as a tmpfs, scoped to the subprocess lifetime.
- The `0644` / `0777` permissions Codex currently produces are explicitly rejected by the
  job-dir permission verifier.

### 12.3 Startup verifier

A verifier (`airgap/credential_verifier.py`) scans the agent container's environment,
`CODEX_HOME/config.toml` (if present), and the job dir for `.codex/auth.json` / `.codex/config.toml`.
If any forbidden secret pattern is present, the job refuses to start.

---

## 13. Control-Plane Egress in Air-Gap Mode

The agent can influence control-plane behavior via fields it writes (`job_meta` etc.). Anything
the control plane then sends outbound is an indirect egress path. In air-gap mode the control
plane disables:

- ntfy notifications
- Pricing/cost fetches (`providers/pricing.py`)
- Skill sync from GitHub
- OAuth callbacks / external provider auth flows
- Any other outbound integration introduced later

A **fully offline deployment** additionally blocks the server container's outbound network at the
**host firewall**. Documented in `docs/AIR_GAPPED.md`; cannot be enforced from inside the
application.

---

## 14. Verification & Attestation

### `make airgap-verify` (CI gate)

Launches a real agent container under full air-gap policy and runs probes:

**Negative probes (must ALL fail / time out):**

- DNS resolve of external domain
- DNS resolve of unique random subdomain (detects DNS-encoded exfil)
- TCP connect to public IPv4
- TCP/UDP/ICMP over IPv6 (if v6 interface present)
- ICMP ping to public IPv4
- `python -c "import urllib.request; urllib.request.urlopen('https://example.com')"`
- `curl`/`wget` to external URL
- `pip install`, `uv pip install`, `cargo fetch`, `git ls-remote` to public remote
- `host.docker.internal` and bridge gateway IP — unreachable
- Attempted Docker API ops from the agent — denied by the airgap socket proxy (§9)
- From **inside the executor container**, attempted connection to *anything* — fails (no network
  namespace egress)
- **Codex CLI probes:** generated Codex command / config has `network_access_enabled=False` and
  `web_search_enabled=False`; `config.toml` does not contain forbidden secrets; `auth.json` is
  not in the job dir
- **Claude SDK probes:** SDK options have web/network built-ins disabled

**Positive probes (must succeed):**

- Reach the local LLM endpoint by IP:port (matching the active provider's egress target)
- Reach the local PubMed mirror

**Service-side probes (must ALL fail) — run from inside the LLM and PubMed containers:**

- Same negative probe set.

### Per-job signed attestation record

For every air-gap job, before the agent starts and at job end, the system produces a JSON
attestation containing:

- Master switch value + all derived invariants (§5)
- `docker network inspect` for the per-job network
- `ip route`, `ip -6 route` inside the agent container
- `nft list ruleset` (or `iptables-save -t filter`) for the per-job rules
- Resolver config
- Image digests for agent, executor, LLM, PubMed, **Codex CLI binary**
- Engine version
- Probe transcripts
- Credential-minimization verifier output (env allowlist applied, `.codex/config.toml` scan,
  `.codex/auth.json` absence)
- Egress registry result for the configured provider, with each target resolved

Signed (job-scoped key) and stored alongside job artifacts. Implementation in
`airgap/attestation.py`.

---

## 15. Local PubMed

`make download-pubmed` + a small eutils-compatible shim. Sibling repo
`openscientist-pubmed-mirror` ships the build pipeline + the shim; this RFC tracks the OS-side
integration only:

- New `PubmedSettings` class with `pubmed_host_path` (mirrors `PHENIX_HOST_PATH` /
  `EXOMISER_HOST_PATH` patterns) and `pubmed_base_url`.
- `job_container/runner.py` bind-mounts `pubmed_host_path` → `/opt/pubmed` read-only when set.
- `literature.py:37` reads `PUBMED_BASE_URL` instead of hardcoded eutils URL.
- `docker-compose.yml` adds the `pubmed-mirror` service when `airgap_mode=True`.
- Mirror satisfies §7 service contract (no outbound, no remote fallback, bounded logs).

---

## 16. Configuration Surface

All new settings default to non-air-gapped behavior (G3). Likely additions:

| Setting (env) | Section | Purpose |
|---|---|---|
| `OPENSCIENTIST_AIR_GAPPED` | new / `ContainerSettings` | master switch (default `false`) |
| `OPENSCIENTIST_AIRGAP_LLM_ADDR` (per-backend) | `ProviderSettings` | IP:port of local LLM |
| `OPENSCIENTIST_AIRGAP_PUBMED_ADDR` | `LiteratureSettings` | IP:port of local PubMed mirror |
| `PUBMED_BASE_URL`, `PUBMED_HOST_PATH` | `LiteratureSettings` | mirror endpoint + host mount |
| `OPENSCIENTIST_AIRGAP_SPARQL_ALLOW` | code-exec config | allowlist of internal SPARQL endpoints |
| `OPENSCIENTIST_AIRGAP_MIN_ENGINE` | new | minimum Docker Engine version |
| `OPENSCIENTIST_AIRGAP_CODEX_BIN_SHA256` | new | pinned Codex CLI binary digest |
| `OPENSCIENTIST_AIRGAP_{BEDROCK,VERTEX}_ENDPOINT` | new (optional) | explicit internal mappings for providers whose SDK doesn't expose endpoint override |

Single validated `air_gapped: bool` is what the rest of the code branches on.

---

## 17. Integration Points (file-level)

Refreshed for the diff-minimization architecture. PR-1 modifies four of Luca's recently-touched
files and adds a new `src/openscientist/airgap/` package.

### 17.1 Modified core files (conditional branches)

- **`src/openscientist/settings.py`** (~30 lines): add `airgap_mode` to `ContainerSettings`; add
  `airgap` block (host/port for LLM, PubMed, optional Bedrock/Vertex internal mappings);
  `get_agent_env()` gains an `active_provider_id` parameter and routes through
  `airgap.env_allowlist.filtered_agent_env()` when `airgap_mode`.
- **`src/openscientist/agent/factory.py`** (~5 lines): when `airgap_mode=True` and provider is
  `CodexCompatible`, instantiate `AirgapCodexAgent` instead of `CodexAgent`. Otherwise unchanged.
- **`src/openscientist/job_container/runner.py`** (~15 lines): airgap-mode conditional that
  (a) bind-mounts the Codex `auth.json` from the host-side secret tmpfs instead of writing to
  `job_dir/.codex/`; (b) sets `CODEX_HOME` to the per-job tmpfs path; (c) applies the §8 hardening
  flags; (d) attaches the container to the per-job internal network (not the default bridge).
- **`src/openscientist/container_manager.py`** (~10 lines): airgap-mode conditional that switches
  the Docker socket path to the airgap proxy (§9). Otherwise direct socket as before.

### 17.2 New `src/openscientist/airgap/` package

- `__init__.py` — package marker.
- `egress_registry.py` — provider-id → deterministic IP:port set; `validate_provider_for_airgap()`
  + `AirGapUnsupportedError` + `AirGapPolicyError`.
- `codex_agent.py` — `AirgapCodexAgent(CodexAgent)` subclass; overrides `_build_mcp_env`,
  `_codex_home`, `_thread_options`.
- `env_allowlist.py` — active-provider-only credential filtering; Codex MCP env construction.
- `firewall.py` — nftables/ip6tables rule apply + teardown per job; captures for attestation.
- `attestation.py` — per-job signed JSON writer + verifier.
- `probes.py` — `airgap-verify` probe set (DNS, TCP, IPv6, ICMP, Codex-config inspection,
  Claude-options inspection, service-side probes).
- `export_boundary.py` — `.codex/*` exclusion + filesystem secret scan.
- `docker_proxy.py` — airgap-only socket-proxy path selection (§9).
- `mcp_filter.py` — airgap-filtered MCP tool list + allowed Claude/Codex built-ins.
- `credential_verifier.py` — startup verifier (§12.3).

### 17.3 Codex agent helper-method refactor (~20 lines)

`src/openscientist/agent/codex_agent.py` gets a small refactor (NOT a behavior change in
non-airgap mode) to expose three helpers that `AirgapCodexAgent` overrides:

- `_build_mcp_env()` — extracted from current inline env-copy logic in `_mcp_env`.
- `_codex_home()` — currently hard-coded; returns the default value.
- `_thread_options()` — extracted from current inline construction.

All three are protected (underscore-prefixed) so they're not part of the public contract; the
public methods `_mcp_env` / `_write_codex_config` / `run()` are unchanged in signature and
behavior.

### 17.4 Other touch points

- `src/openscientist/literature.py:37` — read `PUBMED_BASE_URL` env var instead of hardcoded URL.
- `src/openscientist/code_executor.py:27-57` — keep `requests` etc. removed from import allowlist
  as fail-fast UX (the real boundary is the executor network namespace per §10.2).
- `src/openscientist/code_executor.py:398-436` — SPARQL endpoint validated via
  `airgap.sparql_allowlist`.
- `src/openscientist_tools/server.py:18-25` — MCP tool registration consults
  `airgap.mcp_filter.allowed_mcp_tools(settings)`.
- `src/openscientist/agent/claude_code_agent.py:170-193` — read airgap-filtered SDK built-in
  allowlist from `airgap.mcp_filter`.
- `src/openscientist/job_chat.py` — guard the interactive chat entrypoint with
  `if settings.airgap_mode: refuse_with_message()`.
- `src/openscientist/orchestrator/discovery.py` — invoke attestation writer at job-start and
  job-end when `airgap_mode`.
- `Dockerfile.agent` — pre-bundled SHA256-pinned Codex CLI binary; offline package managers (§8.1).
- `Dockerfile.executor` — fully-isolated network namespace at runtime (§10.2 — implemented as a
  `--network=none` argument in airgap mode, since the executor never needs network).
- Skill ingestion path — disable GitHub fetch; pre-bundle + sign.
- Host firewall management (new): nftables/ip6tables rule application + teardown per job (lives
  in `airgap/firewall.py`).
- `docker-compose.yml` — adds the `pubmed-mirror` service when airgap; image-pin and offline-
  install constraints.
- `Makefile` — `download-pubmed`, `download-codex`, `airgap-verify` targets.

---

## 18. Phased Implementation Plan

### PR-1 — Foundation + the guarantee

**Files modified in Luca's recently-touched code (4 files, ~60 lines total):**

| File | Change |
|---|---|
| `src/openscientist/settings.py` | `airgap_mode` flag + airgap config block; `get_agent_env(active_provider_id)` parameter; airgap-mode routing |
| `src/openscientist/agent/factory.py` | one conditional selecting `AirgapCodexAgent` |
| `src/openscientist/job_container/runner.py` | airgap-mode block: tmpfs Codex auth mount, `CODEX_HOME`, hardening flags, per-job network attach |
| `src/openscientist/container_manager.py` | airgap-mode block: socket path → airgap proxy |

**Files added (new, no conflict):**

| File | Purpose |
|---|---|
| `src/openscientist/airgap/__init__.py` | package marker |
| `src/openscientist/airgap/egress_registry.py` | provider-id → IP:port targets + validation |
| `src/openscientist/airgap/codex_agent.py` | `AirgapCodexAgent` subclass |
| `src/openscientist/airgap/env_allowlist.py` | active-provider-only credential filtering |
| `src/openscientist/airgap/firewall.py` | nftables/ip6tables apply + teardown |
| `src/openscientist/airgap/attestation.py` | per-job signed JSON writer |
| `src/openscientist/airgap/probes.py` | `airgap-verify` probe set |
| `src/openscientist/airgap/export_boundary.py` | `.codex/*` exclusion + secret scan |
| `src/openscientist/airgap/docker_proxy.py` | socket-proxy path selection |
| `src/openscientist/airgap/mcp_filter.py` | airgap-filtered MCP + built-in allowlist |
| `src/openscientist/airgap/credential_verifier.py` | startup verifier |
| `tests/airgap/__init__.py` | test package marker |
| `tests/airgap/test_egress_registry.py` | covers all provider_ids; AirGapUnsupportedError raises |
| `tests/airgap/test_codex_agent_airgap.py` | asserts `network_access_enabled=False`, env allowlist |
| `tests/airgap/test_env_allowlist.py` | active-provider-only filtering |
| `tests/airgap/test_export_boundary.py` | secret scan, `.codex/*` exclusion |

**Codex agent helper-method refactor (1 file, ~20 lines):**

- `src/openscientist/agent/codex_agent.py` — extract three protected helpers for the subclass to
  override. No public API change.

**Total PR-1 diff:** ~60 lines in 4 of Luca's files + ~20 lines extract-helpers in 1 of his files
+ ~750 lines new in `airgap/` + ~580 lines new in `tests/airgap/`. ≈ 1,400 lines, ≈ 95 % in new
files Luca doesn't have to context-switch into.

### PR-2 — Local PubMed

`PUBMED_BASE_URL` threading, `PubmedSettings`, `make download-pubmed`, eutils shim, snapshot IDs,
`docs/AIR_GAPPED_PUBMED.md`, mirror service contract.

### PR-3 — Full executor-spawn extraction

Replaces the airgap-only Docker socket proxy with the full control-plane spawn service from the
v2/v3 RFC. `container_manager.py` and `openscientist_tools/code_exec.py` get the structural
refactor. Non-airgap deployments continue to work; airgap stops using the proxy.

### PR-4 — Operator deployment guide

`docs/AIR_GAPPED.md` end-to-end, host-firewall recipe for fully offline deployment.

---

## 19. Remaining Open Questions

1. **DNS architecture:** static `--add-host` vs. local non-recursive resolver. Both work.
2. **Provider explicit-mapping settings:** the shape of
   `OPENSCIENTIST_AIRGAP_BEDROCK_ENDPOINT` / `_VERTEX_ENDPOINT` — single URL enough or do these
   SDKs need richer config?
3. **Codex `auth.json` provisioning:** cleanest source for the read-only secret mount —
   operator-managed file, K8s secret, Docker secret?
4. **Local PubMed service tech:** eutils shim vs ES/Solr + adapter (the sibling repo currently
   picks SQLite FTS5 + FastAPI shim).
5. **DLP scrubbing in export:** how aggressive a default?
6. **Engine version pin:** minimum patched Engine version (CVE-2024-29018).
7. **Job-scoped DB credentials:** generation/lifecycle (Postgres roles vs. short-lived JWTs).
8. **`docker_proxy.py` implementation choice:** `tecnativa/docker-socket-proxy` image vs. custom
   `socat`+filter wrapper. PR-1 picks one in code.

---

## 20. Residual Risk (honesty statement)

Air-gap mode prevents **unauthorized network connections** from agent / executor / spawned
containers (regardless of backend), verified by per-job attestation and the host firewall. It does
**not** address:

- The **report and artifact export channel** — operator-reviewed and operator-released.
- **What allowlisted local services do with the traffic they receive** — §7 contracts.
- **Indirect channels** via operator interaction with poisoned outputs (link clicks etc.).
- **The post-job interactive chat** — disabled in airgap mode in PR-1 rather than secured; the
  in-container chat is a v2 improvement.
- **Host, kernel, Docker daemon compromise; malicious operator; physical access.**
- **Covert/side channels** (timing, cache, power, resource consumption).
- The **control-plane server's own outbound connections** without an additional host-firewall
  block.
- **Build-time supply chain** of agent/executor base images and the bundled Codex CLI binary
  beyond what SHA256 pinning catches.

The "guarantee" claim should always be cited with §4's precise statement and this section.

---

## 21. Revision Log

**v4 (2026-06-05) — after the fourth Codex review (Review-3), incorporating both the new gaps and
the diff-minimization architecture:**

- **Diff-minimization architecture adopted.** Air-gap-specific logic lives in a new
  `src/openscientist/airgap/` package that *reads* provider/agent metadata rather than *modifying*
  their contracts. The v3 design called for adding `Provider.airgap_egress_targets()` to
  `providers/base.py` and modifying every provider subclass and `CodexAgent`. v4 replaces all of
  that with `airgap/egress_registry.py` (external dispatch table) + `airgap/codex_agent.py`
  (`AirgapCodexAgent` subclass) + a ~20-line helper-extraction refactor of `agent/codex_agent.py`
  whose public API is unchanged. PR-1 touches 4 of Luca's recently-touched files for ~60 lines
  total (settings, factory, runner, container_manager) and adds 7 new production files + 5 new
  test files in `airgap/`. ≈ 95 % of the diff is in new files. See §17 and §18.
- **Active-provider-only credential allowlist** (§12.1). Codex Review-3 noticed
  `settings.get_agent_env()` passes the full provider credential set to every job container
  regardless of which provider is active. v4 requires `get_agent_env(active_provider_id)` and an
  `airgap/env_allowlist.py` module that filters to only the active provider's vars plus the
  airgap-base allowlist.
- **`job_chat.py` post-job chat agent acknowledged and addressed** (§2 stance, §4 vector table,
  §20). v3 didn't name this; v4 disables interactive chat when `airgap_mode=True` with a clear
  operator-facing message. Full in-container chat is a v2 follow-up.
- **`code_executor.py` `exec()` bypass closed at the network layer** (§10.2). The agent's
  `import socket` / `urllib.request` directly in `exec()`'d Python bypasses any `requests`-wrapper
  allowlist. v4 mandates that the executor container has **no egress route at all** — its network
  namespace is fully isolated. The import allowlist remains as fail-fast UX but is no longer the
  security boundary.
- **Option B (Docker socket proxy) reopened as airgap-only intermediate** (§9). The prior calculus
  dismissing socket proxies was based on general-path risk; an airgap-only proxy's blast radius is
  limited to airgap deployments. PR-1 ships the proxy; PR-3 does the full extraction to a
  control-plane spawn service. ~10 conditional lines in `container_manager.py` instead of a
  structural refactor.
- **§17 fully refreshed** to reflect the diff-minimization architecture: most touch points are
  reads from / instantiations of new `airgap/` modules rather than in-place modifications of core
  code.
- **§18 PR-1 spelled out by file** with annotated line-count estimates per file: 4 modified
  Luca-files + 1 helper-extraction refactor + 11 new production files + 5 new test files.

**v3 (2026-06-04) — after Codex Review-2 (against current `origin/main`):**

Codex backend brought into scope throughout; provider-family endpoint contract introduced (as an
in-class `Provider.airgap_egress_targets()` method); Codex `config.toml` env-copy + `auth.json`
permissions identified as new credential exfil paths; Codex CLI runtime + supply chain hardening
added; export boundary extended to `.codex/*` artifacts; §4 vector list expanded for the Codex
backend; §17 file refs refreshed against current main. See `git show a2c6a65` for v2 diff
versus v3.

**v2 (2026-05-29) — after Codex Review-1:**

Guarantee narrowed to "no unauthorized network connections" (from broader "nothing leaves the
box"); OQ#1 resolved to Option A (socket removal); layered network controls; DNS handling;
service contracts for allowlisted local services; output / export boundary section added;
credential minimization section added; control-plane egress disable; per-job signed attestation.
