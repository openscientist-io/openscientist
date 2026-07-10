# Air-Gapped Mode: Deployment Guide

Operational companion to `docs/AIR_GAPPED_MODE_RFC.md`, which defines the
threat model and design. This doc covers what an operator actually has to
stand up to get a working air-gapped deployment — referenced from several
places in the code and RFC (`airgap/docker_proxy.py`,
`settings.py`'s `AirgapSettings.docker_socket_path`) but never written
until now (issue #218).

## 1. Docker socket proxy (RFC section 9)

Air-gapped mode's `execute_code` tool spawns short-lived "executor"
containers to run agent-authored Python. The *agent* container that
requests this must never hold direct access to the real Docker socket —
that would let a compromised or manipulated agent spawn a privileged
sibling container and escape the sandbox entirely (mount the host
filesystem, reach the network, etc.). Air-gapped mode instead routes that
access through a small two-container proxy:

```
agent container --TCP--> validator --(internal-only network)--> haproxy --> /var/run/docker.sock
```

- **`airgap-docker-proxy-haproxy`** holds the real socket. Coarse
  path+method allowlist (`docker/airgap-docker-proxy/haproxy/haproxy.cfg`)
  — six routes only (container create/start/json/list/logs/wait/delete,
  image inspect, ping). Unreachable except from the validator (see the
  `airgap-proxy-backend` network in `docker-compose.yml`, which is
  `internal: true` — no host route, no route to the agent).
- **`airgap-docker-proxy`** (the validator) is what the agent actually
  talks to. Does real JSON-body inspection on
  `POST /containers/create` — rejects `Privileged`, host bind mounts,
  added capabilities, non-`none` `NetworkMode`, and several other
  `HostConfig` fields that could defeat the sandbox (see
  `docker/airgap-docker-proxy/validator/validator.py`'s module docstring
  for the full list and reasoning). Also hard-denies any
  `/containers/{id}/exec` request, closing a gap that a coarse
  path-prefix-only proxy (e.g. `tecnativa/docker-socket-proxy`'s
  `CONTAINERS=1` toggle) would otherwise leave open.

### Bringing it up

```bash
docker compose --profile airgap build airgap-docker-proxy airgap-docker-proxy-haproxy
docker compose --profile airgap up -d airgap-docker-proxy airgap-docker-proxy-haproxy
```

The application's `OPENSCIENTIST_AIRGAP_DOCKER_TCP` setting must point at
the validator service by its compose hostname:

```bash
OPENSCIENTIST_AIRGAP_DOCKER_TCP=airgap-docker-proxy:2375
```

This is the only Docker socket configuration needed — the validator and
haproxy containers are wired to each other automatically via the
`airgap-proxy-backend` network in `docker-compose.yml`; there's no
separate Unix-socket-path setup to do for this deployment shape.

(`AirgapSettings.docker_socket_path` and the corresponding Unix-socket
bind-mount path in `job_container/runner.py` are a second, older
deployment shape for operators who prefer a host-path Unix socket instead
of TCP over the compose network. That path is unused by the proxy in this
doc — if you want that shape, point your own proxy deployment's Unix
socket at `docker_socket_path`'s configured host path instead of using
`docker-compose.yml`'s services here.)

### Restricting which image can run

Set `AIRGAP_DOCKER_PROXY_ALLOWED_IMAGES` (comma-separated) on the
`airgap-docker-proxy` service — defaults to
`OPENSCIENTIST_EXECUTOR_IMAGE` (i.e. `openscientist-executor:latest`) in
`docker-compose.yml`, so a normal deployment needs no extra configuration
here. Leaving it unset entirely disables image-name enforcement (the
validator will accept a `POST /containers/create` for any image) — not
recommended, but documented rather than silently different behavior.

### Why not `tecnativa/docker-socket-proxy`

Considered and rejected. Pulling and inspecting that image's actual
`haproxy.cfg.template` shows it gates only by URL **path** and HTTP
**method**, via env-var toggles per API section (`CONTAINERS=1`,
`POST=1`, etc.) — it never inspects the request **body**. The minimum
toggles needed to unblock `execute_code` (`CONTAINERS=1`, `POST=1`) would
let a `POST /containers/create` body request `Privileged: true`, host
bind mounts, or `NetworkMode: host` — a full sandbox escape, defeating
the entire point of the proxy. This repo's proxy keeps HAProxy itself
(the actual battle-tested reverse-proxy engine) but owns a minimal config
directly, plus a small validator sidecar in front that does the body
inspection tecnativa's wrapper doesn't.

## 2. PubMed literature search: `pubmed-mock` / `pubmed-mirror`

`search_pubmed` needs an internal literature endpoint in air-gapped mode
(`OPENSCIENTIST_AIRGAP_PUBMED_ADDR`). Two options, both compose services
gated behind profiles **not** activated by plain `docker compose up` or
`make start`:

```bash
# Small bundled corpus, good for local dev / testing:
docker compose --profile airgap-mock up -d pubmed-mock

# Full mirror (requires a sibling openscientist-pubmed-mirror checkout
# with a built pubmed.sqlite — see that repo's own docs):
OPENSCIENTIST_PUBMED_DB=/path/to/pubmed.sqlite docker compose --profile airgap-mirror up -d pubmed-mirror
```

Forgetting the `--profile` flag is an easy mistake — `search_pubmed`
will fail with a DNS/connection error indistinguishable at first glance
from the Docker-proxy gap in section 1, since both manifest as "the tool
call failed" in the agent's transcript. See the troubleshooting table
below.

## 3. `OPENSCIENTIST_HOST_PROJECT_DIR`

When the web/main app process itself runs inside a container (the normal
`docker-compose.yml` deployment shape) and spawns *sibling* job containers
via the Docker socket, bind-mount paths for those sibling containers must
be given in **host** filesystem terms, not container-internal terms — the
Docker daemon resolves bind-mount sources against the real host, not
against the web container's own filesystem view. `OPENSCIENTIST_HOST_PROJECT_DIR`
is the translation: set it to the absolute host path of your OpenScientist
checkout (the same directory `docker-compose.yml` lives in).

**Silent-failure mode**: if this is set to the wrong path (e.g. copied
from a different checkout, or from a colleague's `.env`), job containers
still start and often still run — but every file the agent/executor
writes (reports, transcripts, uploaded data) lands in the *wrong* host
directory (whatever `OPENSCIENTIST_HOST_PROJECT_DIR` actually points at),
not the one you're looking at. There's no error; the job appears to
"complete" but the report is missing, because the app looked for it in
the correct directory while the container wrote it to the misconfigured
one. Verify with:

```bash
docker inspect <job-container-name> --format '{{json .Mounts}}' | grep jobs
```

and confirm the host-side path matches your actual checkout.

## 4. Troubleshooting

| Symptom | Likely cause |
|---|---|
| `execute_code` fails every call, agent reports "container unavailable" / DNS failure | Docker socket proxy (section 1) not running, or `OPENSCIENTIST_AIRGAP_DOCKER_TCP` not set / points at the wrong hostname |
| `search_pubmed` fails every call | `pubmed-mock` / `pubmed-mirror` not started — check the compose profile was passed |
| Job completes but the Report tab shows "Report generation failed" / no report visible | `OPENSCIENTIST_HOST_PROJECT_DIR` mismatch (section 3) — report was written to a different host directory than the one you're checking |
| `execute_code` returns 403 for a request that looks legitimate | Check `docker/airgap-docker-proxy/validator/validator.py`'s `_reject_reason` — the request's `HostConfig` likely sets a field on the deny list (see section 1); this is almost certainly the validator doing its job, not a bug |
