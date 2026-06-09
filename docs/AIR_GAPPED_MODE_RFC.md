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
| **`ProviderSettings.get_container_env_vars()` passes ALL provider credentials** to every job regardless of which provider is active | a job using Anthropic still receives `OPENAI_API_KEY`, Foundry token, Bedrock keys, etc. | **active-provider-only credential allowlist** (§12.1) |
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
13. **Active-provider-only credential allowlist.** `ProviderSettings.get_container_env_vars(active_provider_id)`
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

- Forbid `host-gateway` and `extra_hosts`. PR #195's web/agent images add `extra_hosts:
  ["host.docker.internal:host-gateway"]` so the OS server can reach an Ollama daemon running
  on the Docker host. **In air-gap mode this is forbidden** — air-gap deployments point
  `OPENSCIENTIST_AIRGAP_LLM_ADDR` at an explicit internal IP/hostname (e.g. an Ollama instance
  on the per-job internal network or a separate internal-network service); the `host.docker
  .internal` shortcut is replaced with the operator-controlled allowlisted endpoint. The
  runner's air-gap branch must drop the `extra_hosts` parameter regardless of what PR #195
  sets in the non-air-gap path.
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

### 7.4 The local LLM endpoint — operational shape

The previous subsections refer to "the operator-stood-up local LLM endpoint" as an abstract
service contract. This section names the concrete deployment shape the design assumes — answering
the **"where do the tokens come from?"** question that air-gap mode has been hand-waving on.

**The internal LLM endpoint is an OpenAI-compatible API server fronted by an open-weight model.**
The canonical reference today is **`gpt-oss-120b`** (and its 20B sibling), OpenAI's open-weight
release that exposes the `/v1/responses` Responses API with tool calling — exactly the API
contract the Codex CLI subprocess speaks. Serving stacks that satisfy the contract include vLLM,
llama.cpp's HTTP server, TGI, SGLang, and any of the dozens of OpenAI-compatible inference
runtimes. The operator chooses one based on their hardware budget; gpt-oss-120b runs on a single
H100 (or quantized on consumer hardware), gpt-oss-20b on much less. The same pattern works with
any open-weight model that ships a compliant Responses-API surface.

**This isn't speculative.** Luca's in-flight `BedrockOpenAIProvider` (PR #190) runs **the same
gpt-oss-120b model through the same Responses API** at Bedrock's Mantle endpoint. A
self-hosted vLLM serving the same weights at `https://10.0.0.5:8443/v1` is operationally
identical from the Codex CLI's perspective — only the `base_url` and auth surface change. A
``LocalOpenAIProvider`` (or a generalized ``OPENAI_BASE_URL`` override on
``OpenAIDirectProvider``) is a ~30-line copy of `bedrock_openai.py` once it lands.

**Provider class.** The agent reaches the local endpoint through a `CodexCompatible` provider on
the established template (compare `azure_openai.py` and `bedrock_openai.py`):

```python
class LocalOpenAIProvider(CodexCompatible):
    def _base_url(self) -> str:
        return get_settings().airgap.llm_addr  # e.g. "https://10.0.0.5:8443/v1"
    def codex_config_overrides(self) -> list[str]:
        return ["[model_providers.local-openai]",
                f'base_url = "{self._base_url()}"',
                'env_key = "LOCAL_LLM_API_KEY"',
                'wire_api = "responses"',
                "stream_max_retries = 10"]
    def codex_model_name(self) -> str | None:
        return get_settings().provider.model or "gpt-oss-120b"
    def codex_sdk_env(self) -> dict[str, str]:
        key = os.environ.get("LOCAL_LLM_API_KEY")
        return {"LOCAL_LLM_API_KEY": key} if key else {}
```

(The exact class name and id depend on what Luca picks — `LocalOpenAIProvider`,
`OpenAIEndpointProvider`, or just extending `OpenAIDirectProvider` with the override field.
PR-1's `egress_registry.py` keys on the provider id string, so adding the entry when it lands is
a one-line change.)

**Agent backend.** No new backend needed. The path is **CodexAgent → AirgapCodexAgent**, both
already implemented in PR-1. The Codex CLI subprocess sees an OpenAI-compatible endpoint and
behaves identically to its Azure / Bedrock / cloud-OpenAI paths.

**What this constrains.** Three things follow from this shape:

1. **`ClaudeCompatible` providers can't trivially work in air-gap mode.** Anthropic doesn't ship
   Claude weights, so there's no "local Claude" to fall back to. The few Anthropic-API-shimming
   proxies that exist (routing Llama/Qwen through `/v1/messages`) are workable in principle but
   the model behind them is still an open-weight model — so an operator who wants Claude SDK +
   open-weight model is paying the SDK gating cost (§10.3) for no model-quality gain over the
   Codex + open-weight path. PR-1's factory refuses ClaudeCompatible providers in airgap mode
   for this reason (§17), pointing operators at the Codex path instead.
2. **The model is open-weight, not frontier.** gpt-oss-120b is competent but isn't Opus. The
   capability/sovereignty tradeoff is real and documented in §20.
3. **Tool-call reliability is the load-bearing risk.** The OS workflow leans heavily on MCP tool
   calls (`search_pubmed`, `validate_citation`, code execution). Open-weight models vary widely
   in tool-call faithfulness. gpt-oss-120b is well above the bar; smaller models may not be. The
   operator-facing docs should name a tested model floor.

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

PR #195 (the Ollama backend) changes the picture: the upstream `codex` doesn't drive
open-weight models on Ollama, so the agent and web images build the Codex CLI from a fork
(`LucaCappelletti94/codex` branch `open-codex`, commit-pinned) via a multi-stage Docker build that
clones the fork's git repo and runs `cargo build`. The fork carries three load-bearing fixes
(harmony parser flattening, function-form `apply_patch`, web-search capability gate) that PR-1
took for granted as already present.

This is **incompatible with a pure "no network at image-build time" stance**. The realistic
operator guidance is:

- **The image is built once with full network access**, on a non-air-gapped build host. The
  `git clone` + `cargo build` happen there, with the fork pinned to its commit hash (a checksum
  of the source tree, transitively). The resulting image is digest-pinned by the operator
  (`docker image inspect` returns the SHA256 the air-gap deployment will run).
- **The deployed images are immutable**; air-gap deployments install the pre-built image by
  digest and never rebuild. The per-job attestation (§14) records the image digest of the
  running agent and executor containers (already in `AttestationRecord.image_digests`), so the
  CI gate can refuse a job whose image digest doesn't match the deployment manifest.
- **The fork's commit hash, the Rust toolchain version, and the cargo lockfile hash** are recorded
  in the per-job attestation under `codex_cli_digest` (already in the dataclass) plus a new
  `codex_cli_provenance` sub-field that captures the source.

What this **doesn't** address (acknowledged): the operator's build host is itself part of the
trust surface. The signed image digest is the audit anchor; whoever built that image had to
trust the fork's commits, the Rust toolchain, the cargo registry, and every transitive
dependency `cargo build` resolved. That's a real attestation chain that's out of scope for this
RFC — it sits in the operator's CI/CD threat model. A v2 might require a reproducible build
(cargo lockfile + a fixed Rust toolchain digest), but that's a follow-up.

Per-job attestation captures:
- Codex CLI binary digest (SHA256 of the compiled binary inside the image).
- Image digest for the agent and executor containers.
- Fork commit hash and a flag indicating the fork-build path was taken (so a verifier can
  distinguish a pre-bundled binary from a build-host artifact).

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
- **MCP tool gating** (`airgap/mcp_filter.py`, **landed in v4.4**): declarative policy with
  three classifications — `MCP_TOOLS_LOCAL_ONLY` (13 tools always safe in airgap),
  `MCP_TOOLS_NETWORK_DEPENDENT` (`search_pubmed`, allowed only when
  `OPENSCIENTIST_AIRGAP_PUBMED_ADDR` is set — fail-closed), `CLAUDE_BUILTINS_NETWORK`
  (`WebFetch`/`WebSearch`, disabled in airgap). The module is the *declaration*; actual
  enforcement is at the three load-bearing layers (Codex CLI fork's `web_search` gate for
  non-OpenAI providers, executor network-namespace isolation per §10.2, host firewall per
  §6). A live FastMCP-registry sentinel in the test suite catches any new tool added
  without a security review.
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

**What this intentionally breaks in air-gap mode** (operators must accept):

- **SPARQL endpoints reached from `exec()`'d code.** Today the SPARQL execution path in
  `code_executor.py:431` opens HTTP connections to whatever `# ENDPOINT:` the agent writes; in
  air-gap mode the executor has no egress route, so any SPARQL endpoint outside the per-job
  allowlist is unreachable. Internal SPARQL stores remain reachable only if they're added to the
  per-job network and §10.5's `airgap.sparql_allowlist` lists them.
- **External-API Python from the agent's code.** `code_exec.py:90` advertises arbitrary Python
  execution; agent-authored `requests.get(...)` / `urllib.request.urlopen(...)` to public APIs
  silently fails (connection refused / no route to host). The agent must use MCP-registered
  tools (`search_pubmed`, `validate_citation`, the LLM call itself) — those run in the *agent*
  container, are gated by the §7 allowlist, and are reachable.

Both are intentional under the §4 threat model: air-gap mode by definition forbids unauthorized
egress from agent-controlled code. The point is to make this loud, not subtle.

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

Codex Review-3's new finding: the container env-injection path
(`ProviderSettings.get_container_env_vars()` in `settings.py:421`, layered with
`JobContainerRunner._build_container_environment()` in `job_container/runner.py:54`) collects
**all** configured provider credentials and passes them to every job container regardless of the
active provider. An air-gap job using Anthropic still receives `OPENAI_API_KEY`, Foundry token,
Bedrock keys, etc. — every one a potential exfil channel if any allowlisted local service ever
logs inbound credentials by mistake.

Air-gap mode requires:

- The env-injection path takes the active provider id (read from settings) and filters
  accordingly.
- **Only** credentials for the active provider are included in the agent env.
- All other provider credentials are stripped.
- Plus `GITHUB_TOKEN`, `OPENSCIENTIST_SECRET_KEY`, full `DATABASE_URL` (with credentials) all
  stripped; the agent gets a job-scoped least-privilege DB credential instead.
- Codex-specific paths handled per §12.2.

The implementation lives in `airgap/env_allowlist.py`:

```python
# airgap/env_allowlist.py (sketch). Var names verified against settings.py:55-142.
PROVIDER_ENV_VARS = {
    "anthropic":    {"ANTHROPIC_API_KEY",
                     "ANTHROPIC_AUTH_TOKEN",        # for OAuth/CBORG-style auth
                     "CLAUDE_CODE_OAUTH_TOKEN",     # for `claude login`
                     "ANTHROPIC_BASE_URL"},
    "cborg":        {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"},
    "openai":       {"OPENAI_API_KEY"},
    "azure-openai": {"AZURE_OPENAI_API_KEY",
                     "AZURE_OPENAI_RESOURCE",
                     "AZURE_OPENAI_DEPLOYMENT",
                     "AZURE_OPENAI_API_VERSION"},
    "foundry":      {"ANTHROPIC_FOUNDRY_RESOURCE",
                     "ANTHROPIC_FOUNDRY_BASE_URL",
                     "ANTHROPIC_FOUNDRY_API_KEY"},  # was ANTHROPIC_FOUNDRY_AUTH_TOKEN in v4
    # Bedrock and Vertex deferred until §19 OQ#2 resolves.
}

def filtered_agent_env(full_env: dict, active_provider_id: str) -> dict:
    allowed = (BASE_AIRGAP_ENV
               | PROVIDER_ENV_VARS.get(active_provider_id, set())
               | {"OPENSCIENTIST_AIRGAP_LLM_ADDR", "OPENSCIENTIST_AIRGAP_PUBMED_ADDR"})
    return {k: v for k, v in full_env.items() if k in allowed}
```

`ProviderSettings.get_container_env_vars()` gets a one-line conditional: if `airgap_mode`, route
the returned dict through `env_allowlist.filtered_agent_env(env, active_provider_id)` before
`JobContainerRunner._build_container_environment()` consumes it.

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
  `ProviderSettings.get_container_env_vars()` gains an `active_provider_id` parameter and routes
  through `airgap.env_allowlist.filtered_agent_env()` when `airgap_mode`.
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
| `src/openscientist/settings.py` | `airgap_mode` flag + airgap config block; `ProviderSettings.get_container_env_vars(active_provider_id)` parameter; airgap-mode routing |
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
2. **Provider explicit-mapping mechanism — OpenAI / Bedrock / Vertex.** None of these has a
   `*_base_url` field on `ProviderSettings` today (`settings.py:62-142`). The resolution path is
   now clearer for each:
   - **OpenAI:** the air-gap shape (§7.4) is a `CodexCompatible` provider pointing at a local
     OpenAI-compatible server (vLLM serving gpt-oss-120b). This is what Luca is trending toward
     in his Codex-provider series (`BedrockOpenAIProvider` is the latest in pattern). Concrete
     change: either generalize `OpenAIDirectProvider` to read `OPENAI_BASE_URL` (smaller diff,
     opens cloud-OpenAI to airgap rewrites too) or add a dedicated `LocalOpenAIProvider`
     class (clearer deployment intent). Either way the egress registry entry flips from
     `_unsupported` to `_from_url(...)`. Decision can wait for Luca's in-flight work.
   - **Bedrock (Claude):** `ClaudeCompatible`, regional SDK. Air-gap support requires either an
     SDK-level base-URL override or routing through an Anthropic-API proxy. Both are
     significant work and don't help against the model-quality bar (§7.4) — deferred unless a
     concrete operator need surfaces.
   - **Vertex:** same shape as Bedrock-Claude. Same deferral.

   PR-1's egress registry refuses these three providers in air-gap mode until either Luca's
   work lands (OpenAI case) or the explicit deferral is revisited. CBORG / Foundry / Anthropic
   / Azure-OpenAI work today because they already have introspectable URL fields.
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
- **Discovery quality vs. frontier models.** Per §7.4, the air-gap deployment runs an open-weight
  model (gpt-oss-120b being the canonical reference). Frontier models (Claude Opus, GPT-5,
  Gemini Ultra) are markedly stronger at multi-step scientific reasoning and at the tool-call
  faithfulness that OS's MCP-heavy workflow depends on. Operators deploying air-gap mode trade
  capability for sovereignty — they are not getting frontier-model discovery, and should not
  deploy expecting it. The operator-facing docs name a tested model floor and recommend
  benchmark calibration before regulated use.

The "guarantee" claim should always be cited with §4's precise statement and this section.

---

## 21. Revision Log

**v4.5.1 (2026-06-09) — Codex Review-7 fixes + drift cleanup:**

Review-7 audit of the v4.5 codebase surfaced five real bugs (three
critical) and several spots where the design prose had drifted from the
landed implementation. Fixes:

- **B1 — DB credentials stripped from agent env (critical).** The
  `BASE_AIRGAP_ENV` allowlist denied `DATABASE_URL` and
  `OPENSCIENTIST_SECRET_KEY`, so every airgap job failed to start (the
  agent needs both to read its job row). Temporarily allowed through with
  a `TODO(PR-2 / RFC §12.1)` marker pointing at the job-scoped
  least-privilege DB role + per-job derived key mechanism §12.1 calls
  for. Tests in `test_env_allowlist.py`.
- **B2 (Fix 4) — MCP policy fail-opens in subprocess (critical).**
  `_apply_airgap_policy()` in `openscientist_tools/server.py` previously
  silently fail-opened when `get_settings()` raised (e.g. under a
  misconfigured env), so the policy never enforced in airgap mode. Now,
  if `OPENSCIENTIST_AIR_GAPPED` is set, any failure in settings load or
  enforcement re-raises so the MCP server refuses to start. Non-airgap
  retains the silent best-effort fallback. Tests in
  `test_mcp_filter.py::TestApplyAirgapPolicyFailClosed`.
- **B3 (Fix 2) — Docker socket container-side path mismatch (critical).**
  `docker_base_url_for_airgap()` returned the host-side
  `settings.airgap.docker_socket_path` from inside the web container —
  that host path doesn't exist there, so the docker SDK failed to
  connect. Fixed: always return the conventional container-side path
  (`/var/run/docker.sock`); operators mount their host proxy socket to
  that path inside the web container. The host-side setting is now
  documented as a runner-only concern (used to build the agent
  container's mount mapping). Tests in `test_docker_proxy.py`.
- **B4 (Fix 4 / B2) — `PUBMED_BASE_URL` never derived from
  `OPENSCIENTIST_AIRGAP_PUBMED_ADDR`.** The addr was set in settings and
  forwarded into the agent env, but `literature.py` reads
  `PUBMED_BASE_URL` (a full URL), not the addr. The agent then fell
  back to the public NCBI URL — which the airgap firewall blocks,
  making PubMed search a silent dead path. The runner now derives
  `PUBMED_BASE_URL=http://<addr>/entrez/eutils` (matching the public NCBI
  eutils layout), with an explicit operator override via host-side
  `PUBMED_BASE_URL` env. Tests in `test_runner_airgap.py`.
- **B5 (Fix 5 / Review-7 Fix 3) — IPv6/port-less allowlist parsing.**
  `_airgap_allowlist_from_settings()` in `agent/factory.py` used
  `rpartition(':')` to split host/port — that breaks IPv6 literals
  (`[::1]:8443` chops at the wrong colon) and silently drops port-less
  hosts. Now uses `urllib.parse.urlsplit` on a synthesized URL so
  Python's bracket-aware parser does the work, with a documented
  `_DEFAULT_AIRGAP_PORT = 443` fallback for port-less hosts. Tests in
  `test_factory_airgap.py::TestParseAirgapAddr`.

Design-prose drift flagged by Review-7 and clarified here so future
readers don't have to cross-check the code:

- **Setting name: `airgap_mode` → `airgap.enabled`.** The body of this
  RFC names the master switch `settings.airgap_mode` (or
  `ContainerSettings.airgap_mode`) in ~14 places (§16 §17, code sketches,
  prose). Implementation diverged: the master switch lives on a separate
  `AirgapSettings` block as `settings.airgap.enabled` with
  `OPENSCIENTIST_AIR_GAPPED` as its environment alias. Everywhere this
  doc says `airgap_mode`, read `airgap.enabled`. The attestation JSON
  schema *does* keep the field name `airgap_mode: bool` (public
  contract).
- **`ThreadOptions` references are stale (§8.2, §7.1 table).** v4.2/v4.3
  describe the airgap codex agent overriding
  `ThreadOptions(network_access_enabled=False, web_search_enabled=False)`.
  PR #195 (Luca's fork) switched the SDK package from
  `openai_codex_sdk` to `openai-codex` and dropped the `ThreadOptions`
  dataclass — `thread_start` now takes kwargs directly. The current
  `AirgapCodexAgent` overrides `_make_codex()` instead, which is the
  shape PR #195 introduced. The §8.2 code sketch is illustrative of
  intent, not the call shape.
- **`airgap/firewall.py` is PR-2 only.** §17 lists `firewall.py` as a
  PR-1 deliverable; §18 (Phased Implementation Plan) and the rest of the
  PR-1 closeout treat it as deferred. Read §17 as design-scope; firewall
  rules are not in this PR. The host-level network isolation in PR-1 is
  achieved by `network="none"` on the executor container plus the airgap
  Docker socket proxy + the operator-provisioned host firewall (operator
  responsibility, documented in `docs/AIR_GAPPED.md`); `firewall.py`'s
  in-app nftables apply/teardown is a PR-2 enhancement.
- **Makefile targets (`download-pubmed`, `download-codex`,
  `airgap-verify`) are PR-2.** §17 line item promises these as PR-1
  deliverables; they are not present in the Makefile after PR-1. They
  land alongside `firewall.py` in the operator-tooling PR-2.

PR-1 is now complete for every Codex Review-6 *and* Review-7 finding.

**v4.5 (2026-06-09) — Codex Review-6 follow-ups landed:**

Review-6 surfaced 9 real bugs (the security-critical 6 landed in
`771aa66`) plus three GAPs deferred to a follow-up. v4.5 closes the
follow-ups:

- **`execute_code` MCP-tool classification + executor `network="none"`.**
  Review-6 flagged `execute_code` as misclassified in
  `MCP_TOOLS_LOCAL_ONLY` because `code_executor.py` opens SPARQL endpoints
  and imports `requests`. The fix: in air-gap mode, the executor
  container is now spawned with `network="none"` (Docker's null-driver
  null network — no interfaces, no route, no DNS resolver), so the
  agent's `exec()`'d Python can `import socket` all it wants and still
  gets no egress. The classification is now correct because the kernel-
  layer enforcement is in place. `container_manager.py` selects the
  network based on `settings.airgap.enabled`; non-airgap still uses the
  resolved agent network. Tests in
  `tests/airgap/test_container_manager_airgap.py`.
- **`attestation.codex_cli_provenance` field.** Per RFC §8.1 the per-job
  attestation now carries a `codex_cli_provenance: dict[str, str]` field
  for the operator to populate from the Dockerfile-emitted build
  manifest (typical keys: `fork_commit`, `rustc_version`,
  `cargo_lock_hash`, `build_host_id`). The field is empty by default;
  populating it is a deployment-config concern, not a code change. Two
  new tests pin the round-trip + HMAC tamper detection.
- **Production-side wiring of the startup verifier.** Review-6 noted
  that `verify_airgap_startup()` and `run_airgap_probe_set()` existed
  only as test fixtures. The orchestrator's `run_discovery_async` now
  calls a new `_enforce_airgap_startup_policy()` helper between provider
  setup and agent construction. It runs `verify_airgap_startup()` and
  raises `RuntimeError` on any blocking finding (env-allowlist leak,
  secret residue in `job_dir`) before the agent is built. Warning-only
  findings are logged but don't block. Probes are *not* wired here —
  they need to run inside the agent's network namespace and belong to
  the agent-side `airgap-verify` target (RFC §14), separate from this
  orchestrator gate. Tests in
  `tests/airgap/test_orchestrator_startup.py`.

PR-1 is now complete for every Codex Review-6 finding. 304 airgap tests
pass. Mypy clean. Ruff clean.

**v4.4 (2026-06-09) — empirical validation + last additive module:**

- **Tier-3 validation passes against real Ollama on macOS** (`scripts/validate_airgap.py`,
  13/13 checks). Exercises every orchestrator-layer code path against a live Ollama daemon
  serving `gpt-oss:120b`: AirgapSettings construction + model_validator, egress_registry
  resolution for `ollama` and refusal of mismatched allowlists, env_allowlist stripping of
  cross-provider creds, credential_verifier startup gate, factory dispatch returning
  `AirgapCodexAgent`, `_codex_home` relocation outside `job_dir`, `_mcp_env` filtering,
  `_ensure_auth` no-op, attestation sign/verify roundtrip with tamper detection. No Docker
  or fork-built CLI required — runs in <1 s. Useful as a CI smoke gate.
- **Tier-4 validation passes on an M-series Mac** (`scripts/validate_airgap_live.py`). The
  cross-process contract — `AirgapCodexAgent` writes the `config.toml`, launches the
  fork-built Codex CLI subprocess, the binary talks to Ollama's `/v1/responses`, gpt-oss
  returns a token — is now empirically confirmed. The first attempt on a non-M-series Mac
  exhausted system resources mid-prefill (gpt-oss:120b CPU-bound); the M-series run
  surfaced two real bugs that were fixed in the same commit:
  - `scripts/validate_airgap_live.py` hardcoded `gpt-oss:120b`; now reads `OPENSCIENTIST_MODEL`
    from env (gpt-oss:20b works on smaller machines).
  - The script reached for a nonexistent `agent.token_usage` attribute; the real property is
    `agent.total_tokens`, and the script crashed in post-turn reporting right after a
    successful turn.
- **§8.1 cargo-build OOM lesson.** Building Luca's Codex fork inside the agent/web images
  via full-parallelism `cargo build` exceeds Docker Desktop's default ~8 GB VM and gets
  SIGKILL'd on `codex-core`. Both Dockerfiles now cap the build to
  `-j ${CODEX_BUILD_JOBS:-3}`; the env var still lets larger build hosts use more parallelism.
  This is operationally significant for §8.1's "build once with full network access" stance:
  operators on default Docker Desktop need either the `-j 3` cap or to raise the VM's RAM
  ceiling.
- **§10 declarative tool allowlist landed** (`airgap/mcp_filter.py`). Last additive PR-1
  module. Three classifications: `MCP_TOOLS_LOCAL_ONLY` (13 tools, always safe in airgap),
  `MCP_TOOLS_NETWORK_DEPENDENT` (`search_pubmed`, allowed iff
  `OPENSCIENTIST_AIRGAP_PUBMED_ADDR` is set — fail-closed), and `CLAUDE_BUILTINS_NETWORK`
  (`WebFetch`/`WebSearch`, disabled in airgap). The module is a *declaration*; enforcement
  remains at the three load-bearing layers (Codex CLI fork web_search gate, executor
  network-namespace isolation, host firewall). A live FastMCP-registry sentinel in the test
  suite caught a real classification gap during development (`set_job_title` was registered
  but unclassified) — the sentinel will catch any future tool added without a security
  review.
- **§17 refreshed** to include `airgap/mcp_filter.py` in the new-file list. PR-1 is now
  functionally complete at the application layer; the remaining `airgap/firewall.py`
  (host-layer nftables/ip6tables) is PR-2 territory since it can't be unit-tested without
  root on a Linux host.

**v4.3 (2026-06-07) — after Codex Review-5, integrating PR #195 (Luca's Ollama backend):**

Codex did a fifth review pass on the in-flight PR-1 in light of #195. Eight real bugs,
four already fixed in this revision, four blocked on #195 actually merging. The RFC
updates:

- **§7.4 alignment with #195's findings.** No factual correction — the RFC already cites
  gpt-oss-120b as the validated reference. Luca's PR body now empirically confirms the
  claim ("a run made 14 `execute_code` calls and produced a substantive report") and
  reports the negative result ("`qwen2.5-coder:32b` returns the tool call as plain text...
  `devstral:24b` degrades to text under the full tool set"). Worth citing in §7.4 prose
  in a v4.4.
- **§6.2 expanded.** PR #195 adds `extra_hosts: ["host.docker.internal:host-gateway"]` so
  the OS server can reach Ollama on the Docker host. RFC §6.2 already forbids this in
  air-gap mode; the section now spells out what an air-gap deployment uses instead (an
  explicit internal endpoint via `OPENSCIENTIST_AIRGAP_LLM_ADDR`) and notes that the
  runner's air-gap branch must drop `extra_hosts` regardless of what the non-air-gap
  path sets.
- **§8.1 rewritten end-to-end.** The previous "no networked install at build time" stance
  is incompatible with #195's fork-build approach (the upstream Codex CLI doesn't drive
  open-weight models on Ollama; Luca's fork carries three load-bearing fixes that build
  from source via `cargo build`). New stance: the image is built once with full network
  access on a non-air-gap build host, with the fork commit-pinned; the air-gap deployment
  installs the resulting image by digest and never rebuilds. The per-job attestation
  records the agent image digest and a new `codex_cli_provenance` sub-field. The build
  host's own trust surface (Rust toolchain, cargo registry transitive deps) is
  acknowledged as out of scope, sitting in the operator's CI/CD threat model.
- **Egress registry adds `ollama`** as a supported provider (was missing — would have
  raised `AirGapPolicyError` on first run). Provider field is `ollama_base_url`.
- **`BASE_AIRGAP_ENV` adds `OPENSCIENTIST_CODEX_TURN_TIMEOUT`** (PR #195 forwards it to
  the agent container; without it in the allowlist the runner's filter would silently
  strip it and CPU-bound gpt-oss-120b runs would be killed by the default timeout).
- **`PROVIDER_ENV_VARS` adds `"ollama"`** (keyless, `OLLAMA_BASE_URL` + `OLLAMA_MODEL`).
- **Factory's `ClaudeCompatible` airgap-refusal message** now names `ollama` (with
  gpt-oss-120b cited) as the recommended local-model path, alongside the cloud-OpenAI
  alternatives.
- **§11 export boundary scan-everything semantics.** Codex Review-5 caught a real bug
  in `export_boundary.evaluate_export`: `.codex/*` was filtered out *before* the secret
  scan, so a misconfigured run that landed `auth.json` in `job_dir/.codex/` had its
  secret content silently exempted from scanning. The RFC explicitly listed `.codex/
  config.toml` and `.codex/auth.json` as scan targets. The implementation now scans
  every intended file (allowed + excluded) and reports findings in both buckets;
  blocking findings in either refuse the export, but the ZIP still only contains the
  allowed paths. The split lets operators see "the file was caught by exclusion AND its
  contents would have blocked" — a stronger signal than either alone.
- **Attestation `expires_at` field added.** Optional freshness bound. PR #195 raises
  `OPENSCIENTIST_AGENT_TIMEOUT` to 48 h for slow gpt-oss-120b runs, so signed
  attestations have a long valid window; operators can now set `expires_at` for a
  shorter downstream re-verification window without re-signing. `verify()` gains a
  `now=` parameter for testability; a tampered `expires_at` still trips the HMAC.
- **`credential_verifier.verify_env` severity-wins instead of first-match-wins.** Codex
  Review-5 flagged that a caller-supplied custom `rules` list with WARN before BLOCK
  could silently mask the BLOCK. The default ruleset is unaffected, but the loop is now
  order-insensitive: it picks the highest-severity match across all matching rules. One
  finding per var (compactness) is preserved.

Bugs still pending #195 actually merging (rebase-time fixes):

- `airgap/codex_agent.py` imports `openai_codex_sdk.ThreadOptions`; #195 switches to the
  `openai-codex` package and inlines `thread_start(...)`. `AirgapCodexAgent` will fail to
  import after merge. Rebase the subclass onto the new `AsyncCodex` API.
- Real merge conflict in `agent/codex_agent.py` env-overlay region: our `_overlay_job_env`
  collides with #195's rewritten `_mcp_env` + absolute-path `_job_dir`.
- `host.docker.internal:host-gateway` `extra_hosts` entry needs an `if not settings
  .airgap.enabled` guard in `runner.py`.
- The fork's `web_search` capability gate — verify whether our explicit
  `web_search_enabled=False` in `AirgapCodexAgent._thread_options()` is still applicable
  to Luca's `openai-codex` library API, or whether the gate makes the override redundant.

**v4.2 (2026-06-06) — answer the "where do the tokens come from?" question:**

The previous versions treated the "internal LLM endpoint" as an abstract operator-deployed
artifact without naming the concrete shape. v4.2 fills that in based on the trajectory of
Luca's in-flight provider work:

- **New §7.4 "The local LLM endpoint — operational shape".** Air-gap mode runs an
  OpenAI-compatible inference server (vLLM, llama.cpp server, TGI, etc.) serving an open-weight
  model — gpt-oss-120b being the validated reference, mirroring what Luca's
  `BedrockOpenAIProvider` (PR #190) does through Bedrock's Mantle endpoint. The provider class
  is a `CodexCompatible` template ~30 lines long, and the agent backend is the existing
  `AirgapCodexAgent` from PR-1 (no new backend needed). Calls out three consequences:
  ClaudeCompatible can't trivially work (no local Claude weights), the model is open-weight not
  frontier, and tool-call reliability is the load-bearing risk.
- **§19 OQ#2 sharpened.** OpenAI's resolution path is now concrete — generalize
  `OpenAIDirectProvider` to read `OPENAI_BASE_URL` OR add a dedicated `LocalOpenAIProvider`
  (Luca's choice). Bedrock-Claude and Vertex are explicitly deferred unless concrete operator
  need surfaces, since the model-quality bar (§7.4) makes the Codex + open-weight path
  preferable anyway.
- **§20 gains the discovery-quality residual risk.** Air-gap mode trades capability for
  sovereignty. Open-weight models are competent but not frontier. Operators should benchmark
  before regulated use.
- **`egress_registry.py` OpenAI entry's error message** rewritten to point at the
  local-OpenAI-compatible-server resolution rather than the misleading "set OPENAI_BASE_URL"
  (the field doesn't exist yet). Test match string updated accordingly.

No code structure change beyond the error-message rewrite.

**v4.1 (2026-06-06) — amendments after Codex Review-4 vibe-check on v4:**

Minor corrections, no architectural change:

- Egress registry provider id `azure_openai` → `azure-openai` (typo against
  `providers/__init__.py:47`). Code + test updated; regression test added.
- Egress registry `openai`, `bedrock`, `vertex` entries no longer pretend to read
  `openai_base_url` / `airgap_bedrock_endpoint` / `airgap_vertex_endpoint` settings fields
  (none exist on current main). All three now raise `AirGapUnsupportedError` with honest
  messages referencing §19 OQ#2.
- §12.1 `PROVIDER_ENV_VARS` sketch fixed against actual `settings.py:55-142`:
  `ANTHROPIC_FOUNDRY_AUTH_TOKEN` → `ANTHROPIC_FOUNDRY_API_KEY`; added
  `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`, `ANTHROPIC_FOUNDRY_BASE_URL`,
  `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`; Bedrock/Vertex entries deferred
  pending OQ#2.
- §12.1 and §17 references to a nonexistent `settings.get_agent_env()` function corrected to
  the real names — `ProviderSettings.get_container_env_vars()` (`settings.py:421`) and
  `JobContainerRunner._build_container_environment()` (`job_container/runner.py:54`).
- §10.2 gained an explicit "what this intentionally breaks" callout — SPARQL endpoints
  reached from `exec()`'d code and external-API Python both fail in air-gap mode, by
  design. Calling this out loudly so operators know to use MCP-registered tools instead of
  arbitrary HTTP.
- §19 OQ#2 rewritten to name the actual design question (per-provider `*_base_url` fields
  vs. single `OPENSCIENTIST_AIRGAP_LLM_ADDR` redirect) rather than the phantom
  `OPENSCIENTIST_AIRGAP_BEDROCK_ENDPOINT` / `_VERTEX_ENDPOINT` that v4 invented.

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
