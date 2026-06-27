# Prebuilt images (Azure ACR)

OpenScientist's images compile the Codex CLI from Rust, which is heavy and can
hang or OOM Docker on low-memory setups (notably Windows + WSL2). To skip the
local build, pull our prebuilt images from Azure Container Registry instead.

**Registry:** `acrcbraindev.azurecr.io` — images (tag `latest`):

| Image | Purpose |
|-------|---------|
| `openscientist` | main web app |
| `openscientist-executor` | sandboxed code execution |
| `openscientist-agent` | discovery agent |
| `openscientist-base` | base layer (only needed if you build from source) |

## 1. Log in (read-only pull token)

```bash
docker login acrcbraindev.azurecr.io -u cbrain-pull -p <PULL_TOKEN>
```

Ask the infra owner for `<PULL_TOKEN>` — it's stored in Key Vault `kv-cbrain-dev`
as `acr-pull-token-password`. The token is **pull-only** (cannot push or delete).

## 2. Create a local compose overlay

`docker-compose.*.yml` is gitignored, so create this file yourself in the repo
root as `docker-compose.acr.yml`:

```yaml
services:
  openscientist:
    image: acrcbraindev.azurecr.io/openscientist:latest
```

## 3. Point the spawned containers at ACR (in your `.env`)

```bash
OPENSCIENTIST_EXECUTOR_IMAGE=acrcbraindev.azurecr.io/openscientist-executor:latest
OPENSCIENTIST_AGENT_IMAGE=acrcbraindev.azurecr.io/openscientist-agent:latest
```

## 4. Pull and run — no build

```bash
docker compose -f docker-compose.yml -f docker-compose.acr.yml pull
docker compose -f docker-compose.yml -f docker-compose.acr.yml up -d --no-build
```

That's it — no Rust compile, no WSL2 hang.

## Refreshing the images

The four images are built and pushed by the infra owner on the Azure build VM
(rebuild each, then push to `acrcbraindev.azurecr.io` with the `latest` tag).
