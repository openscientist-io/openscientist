# Should OpenScientist speak A2A?

**Status:** Proposal / decision doc — not implemented
**Date:** August 2026

**Short answer:** Yes, as a *server*, and it is cheaper than it looks — the existing
job model is already an A2A task model in all but name. But do not start with the
protocol. Start by closing four gaps in the REST API that any external agent needs
regardless of protocol (feedback, event stream, artifact addressing, per-key budget),
then put a ~400-line A2A adapter on top of the same `JobManager`. Treat the DisMech
curation loop as the first real caller; if that caller does not materialise, stop
after the REST work.

---

## 1. What A2A is

[Agent2Agent (A2A)](https://a2a-protocol.org/latest/specification/) is an open
protocol for one agent to hand work to another agent across an ownership or trust
boundary. Google published it in April 2025 and donated it to the Linux Foundation in
June 2025; v1.0 stabilised in early 2026, with 150+ organisations, SDKs in five
languages, and GA support in Copilot Studio, Azure AI Foundry, Bedrock AgentCore and
Vertex Agent Engine.

The parts that matter for us:

- **Agent Card** — a JSON document at `/.well-known/agent-card.json` describing
  identity, skills, endpoints, security schemes, and capabilities. v1.0 supports
  signed cards.
- **Task** — the unit of work. Server-generated id, an explicit lifecycle
  (`submitted → working → completed | failed | canceled | rejected`, with
  `input-required` and `auth-required` as interrupted states), a message history,
  and output **Artifacts** built from **Parts** (text, inline bytes, or file URIs).
- **Transports** — JSON-RPC 2.0, gRPC, or HTTP+JSON, all with equivalent semantics.
- **Update delivery** — polling (`GetTask`), streaming over SSE
  (`SendStreamingMessage`, `SubscribeToTask`), or **push notifications** to a
  client-registered webhook for disconnected clients.
- **Auth** — API key, HTTP bearer, OAuth 2.0, OIDC, mTLS, declared in the card.

### A2A is not a competitor to MCP

The [official positioning](https://a2a-protocol.org/latest/topics/a2a-and-mcp/) is
that MCP is vertical (a model reaching down to tools and resources) and A2A is
horizontal (an agent partnering with another agent). MCP suits stateless primitives
with well-defined I/O; A2A suits systems that "reason, plan, use multiple tools,
maintain state over longer interactions". The canonical shape is an agent that uses
MCP internally and A2A externally — which is exactly what OpenScientist already is
internally (`openscientist_tools` over MCP), missing only the external half.

This distinction settles a question that would otherwise be open for us: **a
discovery run must not be an MCP tool call.** A run takes minutes to hours, streams
progress, may pause for human input, and produces file artifacts. Modelling it as a
tool call means the caller blocks, holds the result in its context window, cannot
resume after a disconnect, and cannot cancel. A2A's task model exists for precisely
this case.

---

## 2. How well does OpenScientist already fit?

Unusually well. The mapping is close to 1:1, which is the strongest argument in favour
— we would be renaming concepts, not inventing them.

| A2A concept | OpenScientist today | Gap |
| --- | --- | --- |
| Agent Card | — | New. Static, derived from settings + enabled skills. |
| Card `skills[]` | `skills/workflow`, `skills/domain` + `/api/v1/skills` | Cosmetic; we already list and describe them. |
| `Task` | `Job` (`job/types.py`, `JobInfo`) | Naming only. |
| `task.id` | job UUID | None. |
| `task.contextId` | — | No grouping of related jobs into one investigation thread. |
| `submitted` | `pending` / `queued` | None. |
| `working` | `running`, `generating_report` | None. |
| `input-required` | `awaiting_feedback` (coinvestigate mode) | **Semantics exist; no API to answer.** |
| `completed` / `failed` / `canceled` | `completed` / `failed` / `cancelled` | None. |
| `rejected` | — | Needed for "over budget" / "no capacity". |
| Message `Part` (file) | multipart data-file upload on `POST /api/v1/jobs` | Needs URI parts too. |
| `Artifact` | report (`/report`), artifacts zip (`/artifacts`), knowledge-state findings, figures | Not individually addressable. |
| Streaming (SSE) | NiceGUI websocket only; REST is poll-only | **Missing.** Transcript entries are the natural event source. |
| Push notifications | `ntfy.py` (topic push on status change, awaiting-feedback) | Right plumbing, wrong shape — needs per-task webhook config. |
| Security schemes | API keys (bearer, `api/auth.py`), OAuth for humans | Declare in the card; mostly done. |
| Cancellation | `POST /jobs/{id}/cancel` | None. |

Four things are genuinely missing, and **all four are worth building even if we never
ship A2A**, because they are what any external caller needs:

1. **No API for coinvestigate feedback.** `awaiting_feedback` can only be answered
   from the NiceGUI page (`_submit_feedback_and_continue` in `job_detail.py`). An
   external caller can start a run but cannot participate in one, and after 15 minutes
   the run auto-continues without them.
2. **No event stream on the REST API.** Callers must poll `/jobs/{id}/status`. Fine
   for a dashboard, wasteful for an agent that wants to react to a finding.
3. **Artifacts are not individually addressable.** A caller gets one markdown report
   or one zip. It cannot fetch "figure 3" or "the findings JSON" by id, which is what
   a downstream curation pipeline actually wants.
4. **No per-key budget.** Agent-initiated job creation spends real provider money.
   Today the guard is that a human is doing the clicking. `vertex-ai-budget-enforcer`
   exists at the infrastructure layer; there is no per-API-key ceiling above it.

---

## 3. Use cases, ranked by whether they are real

### 3.1 DisMech curation delegating evidence work (the one that justifies this)

[DisMech](https://dismech.monarchinitiative.org/) is a disease-mechanism KB of ~1,300
disorders, YAML-in-git as the source of truth, LinkML-validated, ontology-bound
(MONDO/HPO/GO/CL/Uberon/NCIT), with content generated by Claude Code curation agents
and a human PR review gate. Its validators check schema, verify ontology term ids and
labels, and confirm quoted text appears verbatim in the cited PubMed abstract.

That pipeline is excellent at *literature-derived* mechanism claims and structurally
incapable of *data-derived* ones. A curator agent writing the pathophysiology of a
rare disease routinely reaches a claim of the form "pathway X is dysregulated in
tissue Y" that is asserted in a review but never checked against a public expression,
proteomics or variant dataset. That is an OpenScientist job, not a curation step.

The A2A shape of this:

```
DisMech curation agent (Claude Code, in-repo, PR gate)
   │  message/send  → "Test: is <pathway> dysregulated in <tissue> in <MONDO:id>?
   │                   Data: GEO GSE… ; return findings with effect sizes + citations"
   ▼
OpenScientist A2A server  ──► Job (5 iterations, hypothesis mode on)
   │  status: working …  (SSE, or webhook to the GH Action)
   │  status: input-required  → curator agent answers with scope narrowing
   ▼  status: completed
   Artifacts: report.md, findings.json, figures/, transcript
   │
   ▼
Curator agent writes kb/disorders/<mondo>.yaml, cites task id + artifact digest,
opens PR → human review
```

Why A2A specifically, and not "just call the REST API":

- **The boundary is real.** Two deployments, two trust domains, two budgets, two
  release cycles. That is the exact condition under which A2A stops being ceremony —
  the common criticism of A2A is that it adds nothing *inside* one system, and that
  criticism does not apply here.
- **The interaction is long and interruptible.** A GitHub Action cannot hold an
  hours-long HTTP connection. Push-notification config + `SubscribeToTask` on resume
  is the protocol answering a problem we would otherwise hand-roll.
- **Multiple curation harnesses.** DisMech is driven from local `claude` CLI, from
  claude.ai/code, and from CI. One protocol beats three bespoke clients.
- **Provenance for an AI-curated KB.** A stable task id + artifact digest, quotable in
  the YAML, is exactly the traceability the human PR gate needs: a reviewer can pull
  the full transcript for any data-derived claim. This is the underrated benefit —
  A2A gives us a citation format for machine-generated evidence.
- **It runs in the direction we can control.** DisMech is the client and needs nothing
  from us but an HTTP endpoint; OpenScientist is the server. We ship one side.

> **Caveat:** there is no reference to DisMech anywhere in this repository, and the
> public DisMech material does not mention OpenScientist. The above is the integration
> as it *would* work, inferred from the two systems' public shapes. If OpenScientist
> is already used in DisMech through some other path (a human copying results from
> openscientist.io into a YAML file, most likely), that path is the thing to measure
> before building anything — the value of this work is exactly the cost of that manual
> bridge, multiplied by how often it is crossed.

### 3.2 Data-gravity federation (strong, further out)

Controlled-access clinical or patient-level data cannot leave its institution. The
existing answer is "don't analyse it". The A2A answer is to send the task, not the
data: a partner runs their own OpenScientist against their own data, publishes a
signed Agent Card, and our instance delegates to theirs. Opaque execution — no shared
state, no shared tools — is the property that makes this legally tractable, and it is
the design centre of A2A. This is the use case with the highest scientific ceiling and
the longest lead time, since it needs a partner willing to run the server.

### 3.3 OpenScientist as a client of specialist science agents (medium)

The discovery agent's literature access is `search_pubmed` — one tool, one query, one
list of abstracts. Deep-research agents (Asta, FutureHouse, Edison, and DisMech
already sets env vars for some of these) are agents, not tools: multi-turn,
long-running, opaque. Wrapping them as MCP tools is the wrong shape for the same
reason a discovery run is not a tool call. A single `delegate_to_agent` A2A client
tool in `openscientist_tools`, gated by an allowlist of agent cards, would let the
discovery agent consult any of them without per-vendor plumbing.

**Check before building:** whether those services actually expose A2A endpoints today.
If they only offer REST, this reduces to ordinary API clients and A2A buys nothing.

### 3.4 Cross-backend replication (real, but not an A2A problem)

Running the same question through `claude_code`, `codex` and `omp` backends and
comparing conclusions is a genuinely valuable scientific control. It is also entirely
internal — `JobManager` already does fan-out. Do not reach for a network protocol for
an in-process concern.

### 3.5 Being discoverable to generic orchestrators (cheap, speculative)

Once a card exists, openscientist.io is registerable in Copilot Studio / Bedrock
AgentCore / Vertex Agent Engine catalogues. Near-zero marginal cost once 3.1 ships,
unknown demand. Not a reason to start; a reason not to design ourselves out of it.

---

## 4. The case against

Stated plainly, because it is not weak:

- **Most A2A deployments are still proofs of concept**, and the science-agent
  ecosystem is thinner than the enterprise one. A protocol with no counterparty is a
  second API surface to version, test, secure and keep in sync with the first — and
  the two will drift the moment one gets a feature the other lacks.
- **The REST API may already be enough.** For a single known caller, a documented REST
  API plus the four fixes in §2 covers most of §3.1. A2A earns its place when there is
  a *second* caller, or when the caller is not ours.
- **Cost and consent are the real governance gap.** A2A standardises the message
  envelope, not who pays for the compute, what data the delegated agent may retain, or
  what happens when a partner's agent runs up a five-figure bill. Agent-initiated job
  creation must be attributable to a specific API key with a hard ceiling, and must
  use `rejected` rather than a failed job when the ceiling is hit. Ship the ceiling
  before the endpoint, not after.
- **Do not expose tools over A2A.** `execute_code` is a sandbox-escape surface behind
  a container boundary and an exec token. It stays MCP-internal. The A2A surface is
  "submit an investigation", never "run this code".

---

## 5. Recommendation

**Stage 0 — do this regardless (small, no protocol commitment).**
Close the four gaps, on the existing REST API:
- `POST /api/v1/jobs/{id}/feedback` — answer an `awaiting_feedback` job.
- `GET /api/v1/jobs/{id}/events` — SSE over `TranscriptEntry` + status transitions.
- `GET /api/v1/jobs/{id}/artifacts/{artifact_id}` — address artifacts individually.
- Per-API-key spend ceiling, enforced at job creation.

These are useful to the webapp and to any caller, and they are 90% of the work an A2A
adapter would otherwise have to invent.

**Stage 1 — the adapter, once a first caller is committed.**
Mount the `a2a-sdk` Starlette app on the existing FastAPI app (`A2AStarletteApplication`
mounts cleanly under a path), implement `AgentExecutor.execute`/`cancel` against
`JobManager`, serve a static Agent Card from settings + enabled skills, and map
`JobStatus` to A2A task states per the table in §2. Reuse the API-key bearer auth and
declare it in the card; generalise `ntfy.py` into per-task webhook push. Estimate: a
few hundred lines plus tests, precisely because the domain model already matches. One
job model, two front doors — the adapter must own no state of its own.

**Stage 2 — client side.** `delegate_to_agent` tool with an agent-card allowlist, if
and only if §3.3's precondition holds.

**Stage 3 — federation.** `contextId` for multi-job investigations, signed cards,
partner deployments. Only with a partner.

**The decision gate is Stage 1, and it is not a technical one:** name the first
non-OpenScientist agent that will call it. If that is the DisMech curation agent, this
is worth doing and the estimate is small. If nobody can be named, stop after Stage 0
and revisit when someone can.

---

## References

- [A2A Protocol Specification](https://a2a-protocol.org/latest/specification/)
- [A2A and MCP](https://a2a-protocol.org/latest/topics/a2a-and-mcp/)
- [a2aproject/A2A on GitHub](https://github.com/a2aproject/A2A)
- [Linux Foundation: A2A surpasses 150 organizations](https://www.linuxfoundation.org/press/a2a-protocol-surpasses-150-organizations-lands-in-major-cloud-platforms-and-sees-enterprise-production-use-in-first-year)
- [a2a-sdk (PyPI)](https://pypi.org/project/a2a-sdk/)
- [DisMech — Disorder Mechanisms Knowledge Base](https://dismech.monarchinitiative.org/)
- [monarch-initiative/dismech](https://github.com/monarch-initiative/dismech)
