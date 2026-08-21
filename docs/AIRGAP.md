# Air-gapped deployment

Running OpenScientist with no internet access from the agent containers.

## What "air-gapped" means here

Each job runs in a container behind an nftables firewall with `policy drop` on
output. Only DNS, Postgres, the local execution broker and a local LLM proxy are
reachable. Literature search is served from a local copy of MEDLINE instead of
NCBI.

**Model inference is the one carved-out exception.** The container has no route
to the internet; the proxy forwards inference to a trusted API and holds the
credential, so the container never sees it. With an open-weight model that hop
becomes local once you have inference hardware — nothing else about the setup
changes. A hosted model always needs it.

## Before you start

| | |
|---|---|
| Disk | **~150 GB free for Docker.** The PubMed mirror is ~90 GB in Postgres. |
| Time | **~3.5 h** for the mirror ingest (unattended). |
| Credentials | Your own provider credential — see below. |
| Platform | Images are `linux/amd64`. On Apple Silicon they run emulated and builds are slow. |

Also: Docker, `uv`, Python 3.12+.

**Providers.** Anything whose credential can sit behind the proxy works:
Anthropic, Azure AI Foundry, CBORG, OpenAI with an API key, Bedrock with a
bearer token, Ollama, vLLM, llama.cpp.

Vertex and Bedrock-with-SigV4 sign their own requests, so the container reaches
them directly and must hold the credential. Front those with a fixed private
endpoint (Private Google Access, or a PrivateLink VPC interface endpoint).

OpenAI codex ChatGPT-login cannot be air-gapped — its model turn targets
`chatgpt.com` over a websocket no config redirects. Use an API key instead.

## Install

```bash
git clone <repository-url> && cd openscientist
cp .env.example .env
```

Set in `.env`:

```bash
OPENSCIENTIST_AIRGAPPED=true
OPENSCIENTIST_SECRET_KEY=          # openssl rand -hex 32
OPENSCIENTIST_PROVIDER=foundry     # and that provider's credentials
OPENSCIENTIST_MODEL=claude-opus-4-6
```

```bash
make build
make start
docker compose exec openscientist uv run alembic upgrade head
```

## Load the PubMed mirror

Air-gapped mode serves literature from this mirror, so searches return nothing
until it is loaded:

```bash
docker compose exec openscientist uv run python -m openscientist.pubmed_mirror
```

Roughly 40M articles, ~3.5 h, ~90 GB.

`--limit-files N` exists for smoke tests, but a partial mirror is worse than no
mirror: it loads an arbitrary slice, so real queries return zero hits and the
agent reports no literature rather than an error.

## Log in

The UI needs an authenticated session. For a local evaluation, use the built-in
mock login rather than registering an OAuth app — add to `.env`:

```bash
OPENSCIENTIST_DEV_MODE=true
```

then `make restart` and open `http://localhost:8080/auth/mock/admin-login`. That
signs you in as `admin@mock.local`, auto-approved.

**Local evaluation only.** These routes return 404 unless `OPENSCIENTIST_DEV_MODE`
is set, and anyone who can reach the port can sign in as admin. For a real
deployment configure a proper OAuth provider instead.

Dev mode also turns on uvicorn auto-reload, so editing anything under `src/`
restarts the app — and a restart cancels running jobs.

## Verify

UI at `http://localhost:8080`. Submit a job, then while it runs:

```bash
C=$(docker ps --format '{{.Names}}' | grep openscientist-agent)
docker inspect "$C" --format '{{.HostConfig.CapAdd}}'     # [NET_ADMIN]
docker exec "$C" curl -s -m 8 -o /dev/null -w '%{http_code}\n' \
  https://eutils.ncbi.nlm.nih.gov                          # 000 — blocked
```

A `000` (or a `curl` failure) is the firewall working. Anything else means the
container has egress and you are not air-gapped.
