# Code Review and Change Governance

How a change gets from a working copy into a deployed environment, and what must be true at each step.

This document consolidates the governance rules that already exist in the repository — [`../CONTRIBUTING.md`](../CONTRIBUTING.md), [`../.github/PULL_REQUEST_TEMPLATE.md`](../.github/PULL_REQUEST_TEMPLATE.md), `.github/workflows/`, `.pre-commit-config.yaml`, and `.github/CODEOWNERS`. It does not introduce new policy. Where a control is not yet in place, that is stated plainly rather than described as if it were.

Related: [`ENVIRONMENTS.md`](ENVIRONMENTS.md), [`CICD.md`](CICD.md), [`QA.md`](QA.md).

## Principles

1. **All changes go through a pull request.** Direct pushes to `main` are blocked locally by the `no-direct-push-to-main` pre-push hook (`tools/check_no_direct_push_to_main.sh`).
2. **Automated gates run before human review is meaningful.** Contributors are expected to have lint, types, and tests passing locally before requesting review.
3. **Merging to `main` ships to production.** Review depth should reflect that.

## Branch Strategy

### Working branches

Created from `main`, using the prefixes defined in `CONTRIBUTING.md`:

| Prefix | Use |
|---|---|
| `feat/` | New features |
| `fix/` | Bug fixes |
| `refactor/` | Code refactoring |
| `docs/` | Documentation |
| `test/` | Test additions/fixes |
| `hotfix/` | Emergency production fixes |

Names are lowercase, hyphenated, descriptive, and roughly 50 characters or less (`fix/job-status-display`, not `fix/bug`).

### Long-lived environment branches

| Branch | Deploys to |
|---|---|
| `development` | Development environment |
| `staging` | Staging environment |
| `main` | **Production** |

Promotion runs `development → staging → main (production)`. There is no `production` branch; `main` is the production branch. Full mapping in [`ENVIRONMENTS.md`](ENVIRONMENTS.md).

## Commit Conventions

- Atomic, logical commits while working.
- Conventional-style messages where practical: `fix(job): prevent crash on missing provider config`.
- PRs are **squash-merged**, so the PR title becomes the final commit message on the target branch. Titles should be descriptive.

## Opening a Pull Request

From `CONTRIBUTING.md` and the PR template:

1. Push the branch and open a PR against the appropriate target branch.
2. Complete the PR template: description, linked issue, type of change, test plan, self-review checklist, and any breaking changes.
3. Make sure the local quality gates pass first.
4. Request review from at least one team member — two for promotion PRs.
5. Address feedback with new commits; avoid force-pushing over review history mid-review.
6. Once approved and CI is green, a maintainer merges.

Contributors cannot approve their own PR. `CONTRIBUTING.md` records a target of responding to review requests within 48 hours, and 24 hours for promotion PRs.

## Automated Gates

Every pull request runs the CI workflow. The full job list and what each enforces is in [`CICD.md`](CICD.md); in summary:

| Category | Gate |
|---|---|
| Style | `ruff check`, `ruff format --check` |
| Types | `mypy src/openscientist/ tests/` |
| Tests | pytest against a real PostgreSQL service |
| Coverage | 75% absolute floor; no more than a 0.5-point drop against `main` |
| Secrets | gitleaks over the PR commit range |
| Dependencies | `pip-audit`; dependency review failing on high severity |
| Containers | hadolint on all Dockerfiles; base and executor images built when Docker-relevant paths change |

Locally, pre-commit runs ruff, mypy, a reduced pytest selection, gitleaks, and repository-hygiene hooks. It is a fast approximation of CI, not a substitute for it.

Which checks are *required* before a merge is allowed is configured by GitHub branch protection, which lives in repository settings and is not represented in this repository.

## Testing Expectations

- New behaviour is expected to come with tests; the coverage-delta gate makes an uncovered change visible.
- The PR template requires a Test Plan describing how the change was verified, including commands run and results.
- Tests should use real database objects rather than mock classes that bypass the database; see [`QA.md`](QA.md) and [`../CLAUDE.md`](../CLAUDE.md).

## Documentation Expectations

- The PR self-review checklist requires documentation to be updated when behaviour changes.
- Changes to configuration surfaces should be reflected in [`../.env.example`](../.env.example), which is the canonical environment-variable reference.
- Changes to architecture, deployment, environments, CI/CD, QA, or security posture should be reflected in the corresponding document under `docs/` rather than only in the PR description.

## Security Checks

- **Never commit secrets.** No API keys, credentials, or `.env.production` / `.env.staging` values. `CONTRIBUTING.md` instructs contributors who accidentally commit a secret to notify a Maintainer so it can be rotated, rather than quietly deleting the commit.
- Secret scanning runs both locally (pre-commit) and in CI (over the PR commit range).
- Dependency vulnerabilities are gated by `pip-audit` and dependency review, and kept current by weekly Dependabot PRs.
- Standing security findings and their remediation status are tracked in [`SECURITY_REVIEW.md`](SECURITY_REVIEW.md).

## Protected and Sensitive Paths

`CONTRIBUTING.md` asks that changes to protected paths — database, security, deployment configuration, and CI — get sign-off from the relevant `CODEOWNERS` owner.

**Current state:** `.github/CODEOWNERS` is intentionally empty. It contains only commented examples and enforces no ownership rules, so no review is automatically requested for these paths today. Until owners are defined, treat the expectation as a convention that reviewers apply manually: changes touching `src/openscientist/database/`, `src/openscientist/auth/`, `docker-compose.yml`, the Dockerfiles, or `.github/workflows/` warrant a reviewer familiar with that area.

Populating `CODEOWNERS` is a repository-configuration task and is deliberately not done as part of documentation work.

## Breaking Changes

- The PR template has a dedicated **Breaking Changes** section; call them out there rather than only in the description.
- Because the PR title becomes the squashed commit message, breaking changes should be evident from the title.
- For schema changes, note that migration history is immutable: applied migrations are never edited, split, squashed, or rewritten, and all schema changes are new forward revisions from the current head. See the [migrations README](../src/openscientist/database/migrations/README.md).
- Because CD does not apply migrations automatically (see [`CICD.md`](CICD.md)), a breaking schema change needs an explicit operator step at release time and should say so in the PR.

## Promotion Pull Requests

The PR template defines additional requirements for promotion PRs (`development → staging`, `staging → main`):

- Risk assessment included
- Rollback plan documented, for major changes
- Staging validation checklist complete and linked, for production PRs
- QA sign-off obtained, for production PRs

`CONTRIBUTING.md` additionally asks for two reviewers on promotion PRs and a faster review turnaround.

## Upstream Sync (public ↔ private fork)

OpenScientist is developed as a public open-source project, with the core team currently working against a private fork. Per `CONTRIBUTING.md`:

- Changes flowing **from the private fork to public upstream** are reviewed by a Maintainer specifically for secrets, internal infrastructure references, and proprietary content before publication.
- Changes flowing **from public upstream into the private fork** are isolated on a `sync/upstream-<date>` branch and go through the standard PR/review process before touching `main` — never merged directly into `staging` or `main`.

## Not Defined in This Repository

Stated explicitly so these are not assumed:

- Named individual approvers or owning teams (`CODEOWNERS` is empty).
- A defect severity taxonomy, triage rota, or escalation path.
- Release cadence or change-freeze windows.
- Formal sign-off authority for production releases beyond the PR template's QA sign-off checkbox.
