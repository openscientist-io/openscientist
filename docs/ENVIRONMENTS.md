# Environments and Promotion Model

The three deployed environments, how code reaches each one, and how their configuration differs.

Everything below is derived from the workflows in `.github/workflows/`, `docker-compose.yml`, and `src/openscientist/settings.py`. Where something cannot be verified from the repository (because it lives in per-VM configuration or GitHub secrets), that is stated explicitly rather than guessed.

Related: [`CICD.md`](CICD.md) for the pipelines, [`DEPLOYMENT.md`](DEPLOYMENT.md) for the deployment mechanics, [`code-review-governance.md`](code-review-governance.md) for how changes are reviewed.

## Environment Matrix

| | Development | Staging | Production |
|---|---|---|---|
| Branch | `development` | `staging` | `main` |
| Workflow | `.github/workflows/deploy-dev.yml` | `.github/workflows/deploy-staging.yml` | `.github/workflows/deploy-prod.yml` |
| Trigger | push to `development`, or manual | push to `staging`, or manual | push to `main`, or manual |
| Azure resource group | `rg-openscientist-dev` | `rg-openscientist-staging` | `rg-openscientist-prod` |
| Azure VM | `vm-openscientist-dev` | `vm-openscientist-stg` | `vm-openscientist-prod` |
| Image tag | `dev-<git-sha>` | `staging-<git-sha>` | `prod-<git-sha>` |
| Registry | Azure Container Registry (`secrets.ACR_NAME`) | same | same |

All three workflows also accept `workflow_dispatch`, so any environment can be redeployed manually from its branch without a new commit.

## `main` is the Production Branch

This is the single most important thing to know about the model: **merging to `main` deploys to production.** There is no `production` branch, and `main` is not a staging area.

The promotion direction is:

```text
feature branch  →  development  →  staging  →  main (production)
```

A change is expected to have been exercised in the development and staging environments before it reaches `main`, because `main` is the last stop rather than the first.

## What a Deployment Does

All three workflows follow the same shape:

1. Check out the repository.
2. Authenticate to Azure (`azure/login@v2` with `secrets.AZURE_CREDENTIALS`).
3. Rewrite the main `Dockerfile`'s unqualified `FROM openscientist-base:latest` to the ACR-qualified name.
4. Build `openscientist-base:latest` and `openscientist:<env>-<sha>` **server-side in ACR** with `az acr build`. Building in ACR rather than on the runner avoids the memory exhaustion the Rust Codex build causes on small hosts.
5. Mint a short-lived ACR access token and invoke `az vm run-command` on the target VM to:
   - `docker login` to the registry with that token,
   - rewrite the `image:` line in the VM's `/opt/openscientist/docker-compose.yml` to the new tag,
   - `docker compose pull openscientist && docker compose up -d openscientist`.

Only the `openscientist` web service is pulled and restarted. Postgres and the socket proxy are left running.

### Deployment serialization

Each workflow declares `concurrency.group: ${{ github.workflow }}` with `cancel-in-progress: false`. `az vm run-command` permits only one execution per VM at a time, so overlapping runs (for example a batch of merges) would otherwise collide with a Conflict error. Queuing them instead means a deploy is never cancelled part-way through.

### The VM-side Compose file

The file the VMs run, `/opt/openscientist/docker-compose.yml`, is **not** the repository's `docker-compose.yml`. The repository version builds the web image locally (`build: .`); the VM version references a published `image:` that the deploy step rewrites per release. The VM copy is maintained on the host and is not tracked here.

Practical implication: changes to the repository's `docker-compose.yml` — new services, changed mounts, changed environment wiring — do **not** propagate to the deployed environments automatically. They have to be applied to each VM's Compose file separately.

## Configuration and Secrets Separation

| Layer | Where it lives | Notes |
|---|---|---|
| Application configuration | `.env` on each VM | Not in the repository. [`.env.example`](../.env.example) documents every supported variable. |
| Deployment credentials | GitHub Actions secrets | `AZURE_CREDENTIALS`, `ACR_NAME`. |
| Registry pull credentials | Short-lived ACR token at deploy time; `OPENSCIENTIST_DOCKER_CONFIG` for the app's own image pulls | The deploy token is minted per run via `az acr login --expose-token`. |
| Database credentials | `POSTGRES_*` / `DATABASE_URL` / `ADMIN_DATABASE_URL` per environment | Under Compose, `DATABASE_URL` and `ADMIN_DATABASE_URL` are constructed from `POSTGRES_*` and the `postgres` service hostname. |
| Master secret | `OPENSCIENTIST_SECRET_KEY` per environment | Derives session storage and token-encryption keys. Environments should not share a value; rotating it invalidates existing sessions and encrypted tokens. |

Never commit `.env`, `.env.staging`, or `.env.production`.

## Environment Identity in the Application

`OPENSCIENTIST_ENVIRONMENT` tells the application which environment it is running as. `AppEnvironment` (`src/openscientist/settings.py`) defines exactly two members:

| Value | Behaviour |
|---|---|
| `development` | Default. Permits `OPENSCIENTIST_DEV_MODE=true` (mock OAuth and other development features). |
| `production` | Rejects `OPENSCIENTIST_DEV_MODE=true` at startup. |

`ADMIN_DATABASE_URL` is governed separately, by `OPENSCIENTIST_DEV_MODE` rather than by `OPENSCIENTIST_ENVIRONMENT`: startup fails when it is unset and dev mode is off, and falls back to `DATABASE_URL` with a warning only when dev mode is on. Because `OPENSCIENTIST_DEV_MODE` defaults to `false`, `OPENSCIENTIST_ENVIRONMENT=development` on its own still requires `ADMIN_DATABASE_URL`. Setting `OPENSCIENTIST_ENVIRONMENT=production` requires it transitively, since production forbids dev mode.

> **Engineering follow-up — staging has no enum value of its own.**
>
> There is a staging branch, a staging workflow, a staging resource group, and a staging VM, but `AppEnvironment` has no `staging` member. The staging deployment must therefore run with `OPENSCIENTIST_ENVIRONMENT` set to either `development` or `production`, and which one it uses is set in that VM's `.env` — it is not visible in this repository and is not asserted here.
>
> This matters because the two values are not equivalent for a pre-production environment: `development` would permit `OPENSCIENTIST_DEV_MODE=true` (mock OAuth, and with it the `ADMIN_DATABASE_URL` fallback), while `production` forbids dev mode outright. Staging is most faithful to production when it runs as `production`.
>
> Resolving this — either by adding an explicit `staging` member with defined semantics, or by documenting a deliberate convention — is a code change and is out of scope for documentation. Until then, verify the value directly on the staging VM before relying on staging as a production rehearsal.

## Database Migrations Across Environments

None of the three deployment workflows runs `alembic upgrade head`, and the application does not apply migrations at startup — it only verifies at startup that Row-Level Security and the `openscientist_app` role are correctly configured, logging errors if they are not.

> **Engineering follow-up.** Schema changes currently require an operator to apply migrations on the target VM after deployment:
>
> ```bash
> cd /opt/openscientist && docker compose exec openscientist alembic upgrade head
> ```
>
> The older SSH-based `make deploy` path does run migrations as an explicit step, so the two deployment routes behave differently. This is recorded as a known gap; the workflows are not modified here.

Because the schema and the running image are updated independently, prefer backwards-compatible migrations so that a rollback to the previous image tag does not require a schema downgrade.

## Rollback

Images are tagged per commit, so every previous release remains addressable in ACR. Rolling an environment back means redeploying an earlier tag — either by re-running the corresponding workflow from the earlier commit via `workflow_dispatch`, or by repointing the `image:` line in that VM's Compose file to the previous `<env>-<sha>` and running `docker compose up -d openscientist`.

Rolling back across a schema change needs care: the previous image may not be compatible with the migrated schema. See [`MAINTENANCE.md`](MAINTENANCE.md).

## Local Development

Local development is not one of the deployed environments. It runs the repository's own `docker-compose.yml` with images built by `make build` (or the prebuilt ACR images described in [`ACR_IMAGES.md`](ACR_IMAGES.md)), with `OPENSCIENTIST_ENVIRONMENT=development`. Setup instructions are in [`../CONTRIBUTING.md`](../CONTRIBUTING.md) and [`DEPLOYMENT.md`](DEPLOYMENT.md).
