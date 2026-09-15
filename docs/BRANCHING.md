# OpenScientist Branching Strategy & Promotion Rules

> Source: internal documentation, converted from the shared drive. Keep this file as the canonical version.

## Overview
This document defines the formal branching strategy for OpenScientist development, including branch structures, promotion workflows, and governance rules for code moving from development through staging to production environments.
- **Scope:** This applies to all code changes in the OpenScientist repository and its deployment pipeline.

## 1. Branch Structure

### 1.1 Primary Branches
The repository maintains three permanent branches:

| **Branch** | **Purpose** | **Protection** | **Deployment** |
|---|---|---|---|
| **development** | Development & integration | Requires PR + approvals | Dev |
| **staging** | Pre-production validation | Requires PR + approvals | Staging (manual) |
| **main** | Live deployment | Requires PR + approvals + QA sign-off | Production (manual) |

### 1.2 Feature Branches
Temporary branches created from main for development work:
**Naming Convention:**
- feat/ — New features
- fix/ — Bug fixes
- refactor/ — Code refactoring
- docs/ — Documentation updates
- test/ — Test additions/fixes
**Naming Rules:**
- Lowercase with hyphens: feat/cost-tracking-api
- Descriptive, not generic: fix/job-status-display, not fix/bug
- Max 50 characters recommended
**Scope & Lifetime:**
- Should represent a single logical change (one PR)
- Deleted after merge to main (auto-cleanup via GitHub)

## 2. Development Workflow

### 2.1 Standard Development Flow
┌────────────────┐
│   development  │ (Integration branch)
└────┬───────────┘
│
├─→ feat/add-budget-tracking (PR → main)
│
├─→ fix/job-crash-on-timeout (PR → main)
│
└─→ refactor/extract-job-manager (PR → main)

### 2.2 Creating a Feature Branch
```bash
# Ensure development is up to date
git fetch origin
git checkout development
git pull origin development
# Create feature branch from development
git checkout -b feat/my-new-feature
# Work locally
# ... make commits ...
# Push to remote
git push origin feat/my-new-feature
```

### 2.3 Submitting a PR
- Push feature branch to remote.
- Open PR on GitHub targeting development.
- Fill in the PR template with description and test plan.
- Ensure CI passes (automatic via GitHub Actions).
- Request review from 1+ team members.
- Address feedback and push new commits.
- Once approved, **squash and merge** to development.
**Commit Strategy:**
- Create logical, atomic commits on feature branch.
- Squash to single commit when merging to development.
- PR title becomes the final commit message.

### 2.4 Code Review Gates
**PR Requirements:**
- CI tests pass (linting, type checking, unit tests)
- Code coverage maintained (≥60%)
- At least 1 approval from team member
- No unresolved conversations
**Reviewer Responsibility:**
- Verify correctness and adherence to guidelines.
- Test locally if change affects runtime behavior.
- Approve only when satisfied with quality.

## 3. Promotion to Staging

### 3.1 Promotion Criteria
Code is **eligible to promote from** **development** **to** **staging** when:
- Merged to development (has passed code review)
- CI pipeline passes completely
- All automated tests pass with ≥60% coverage
- Deployment-blocking issues are resolved
- No known critical bugs in the feature

### 3.2 Promotion Process
**Manual promotion** (team lead or release manager):
```bash
# Ensure development is current
git fetch origin
git checkout development
git pull origin development
# Create staging integration PR
git checkout staging
git pull origin staging
git merge --no-ff main -m "chore: promote development to staging"
# Review diff
git log origin/staging..development  # See what's new
# Push to staging
git push origin staging
**Or via GitHub UI:**
- Open new PR: development→ staging
- Title: chore: promote development to staging (v0.1.2)
- Description: List major changes and risk assessment
- Request review from team
- Once approved, merge with **Create a merge commit**
```

### 3.3 Staging Deployment
After code is merged to staging, deploy using:
```bash
# Pull latest
git fetch origin
git checkout staging
git pull origin staging
# Build and deploy to staging environment
make rebuild COMPOSE_FILE=docker-compose.staging.yml
# Run migrations if needed
docker compose -f docker-compose.staging.yml exec openscientist \
alembic upgrade head
**Staging deployment requires:**
- Explicit team action (not automatic).
- Verification that all containers start successfully.
- Checking application logs for startup errors.
```

### 3.4 Staging Validation
**Testing Window:** Minimum 24 hours in staging before production promotion.
**Validation Checklist:**
- [ ] Application starts without errors
- [ ] Critical user workflows function end-to-end
- [ ] Database migrations apply cleanly
- [ ] No new errors in application logs
- [ ] Job execution completes successfully
- [ ] Performance is acceptable (no regressions)

## 4. Promotion to Production

### 4.1 Production Promotion Criteria
Code is **eligible for production** when:
- Merged to staging (code review completed)
- Validation in staging complete (≥24 hours)
- No blocking issues found in staging
- Release notes prepared
- Rollback plan documented (for major changes)
- **QA sign-off or team lead approval**

### 4.2 Production Promotion Process
**Production release** (requires team lead authorization):
```bash
# Ensure staging is current
git fetch origin
git checkout staging
git pull origin staging
# Create production integration PR
git checkout main
git pull origin main
git merge --no-ff staging -m "chore: release v0.1.2
Major features:
- Budget tracking API
- Improved job error messages
Bug fixes:
- Fixed stale consensus in regeneration
- Fixed timeout handling in job executor
QA sign-off: @qa-team
"
# Review differences from last release
git log origin/main..staging
# Push to production
git push origin main
**Production release via GitHub:**
- Open PR: staging → main
- Title: chore: release v0.1.2 to main
- Description: Summary of changes, testing results, risk assessment, rollback plan, and approvals.
- Tag commit with version: git tag v0.1.2
- Merge with **Create a merge commit**
```

### 4.3 Production Deployment
**Deployment Window:** No production deploys during peak usage hours. Notify stakeholders 24 hours in advance. Keep the team available.
```bash
# On production server
git fetch origin
git checkout production
git pull origin production
# Build and restart services
make rebuild
# Run migrations (if any)
docker compose exec openscientist alembic upgrade head
# Verify deployment
docker compose logs -f openscientist
# Monitor for errors (first 30 minutes)
```

### 4.4 Production Validation
**Post-Deployment Verification Checklist:**
- [ ] Application started successfully
- [ ] No errors in system logs
- [ ] API endpoints responding normally
- [ ] Database queries executing without issues
- [ ] Cost tracking is accurate
- [ ] No data loss or corruption detected
- [ ] Performance metrics stable

## 5. Hotfixes & Emergency Patches

### 5.1 When to Use Hotfixes
Use hotfix flow when a production bug affects users, cannot wait for the next release cycle, is minimal/low-risk, and can be verified in staging before production.

### 5.2 Hotfix Process
```bash
# Create hotfix from production
git fetch origin
git checkout main
git pull origin main
git checkout -b hotfix/critical-job-crash
# Make minimal fix, commit, push
git commit -m "fix(job): prevent crash on missing provider config"
git push origin hotfix/critical-job-crash
# Open PR to production (Expedited review)
# Backport to development and staging
git checkout development
git pull origin development
git merge --no-ff production -m "chore: backport hotfix from production"
git push origin development
git checkout staging
git pull origin staging
git merge --no-ff main -m "chore: backport hotfix to staging"
git push origin staging
```

## 6. Branch Protection Rules
All permanent branches (main, staging, production) enforce:
- **Require pull request reviews before merging:** Dismisses stale approvals on new commits; requires code owner sign-off.
- **Require branches to be up to date before merging.**
- **Restrict push access:** Only maintainers can force-push in emergencies.

## 7. Release Management

### 7.1 Versioning
Follow:
- **MAJOR** (0.1 → 1.0): Breaking changes to API or database schema.
- **MINOR** (0.1 → 0.2): New features, backwards compatible.
- **PATCH** (0.1.0 → 0.1.1): Bug fixes only.

### 7.2 Release Cycle
Planned releases occur every 2 weeks (or as needed) using a 4-step workflow: **Prepare** (Release branch/Changelog) → **Stage & Validate** (24+ hours on staging) → **Release** (Merge to production + Tag) → **Post-Release** (Monitor logs).

### 7.3 CHANGELOG Format
File: CHANGELOG.md
```markdown
## [v0.1.2] - 2026-07-09
### Added
- Cost tracking API endpoint
- Budget display in job detail page
### Fixed
- Stale consensus handling in regeneration
- Job executor timeout edge case
### Changed
- Improved error messages for job failures
### Removed
- Deprecated v1 endpoint (use v2 instead)
```

## 8. Rollback Strategy

### 8.1 When to Rollback
Rollback production if critical bugs prevent operation, data corruption is detected, major performance regressions occur, or a security vulnerability is introduced.

### 8.2 Rollback Process
```bash
# Revert to previous stable version
git log --oneline production | head
# Identify last known good commit and revert
git checkout main
git pull origin main
git revert <commit-hash>
git push origin main
# Deploy reverted version
make rebuild
```

## 9. Multi-Environment Configuration

### 9.1 Environment-Specific Files
.env                           # Local development (gitignored)
.env.staging                   # Staging environment config
.env.production                # Production environment config (encrypted)
docker-compose.yml             # Development containers
docker-compose.staging.yml     # Staging containers
docker-compose.production.yml  # Production containers
Makefile                       # Build/deploy commands

### 9.2 Deployment Configuration

| **Variable** | **Dev** | **Staging** | **Production** |
|---|---|---|---|
| OPENSCIENTIST_PROVIDER | ollama (local) | cborg | cborg |
| DATABASE_URL | localhost | staging.db | prod.db |
| OPENSCIENTIST_DEV_MODE | true | false | false |
| DEBUG | true | false | false |
| MAX_CONCURRENT_JOBS | 1 | 2 | 4 |

## 10. CI/CD Pipeline

### 10.1 Automated Checks
- **On every PR:** Lint (ruff), format (ruff format), type check (mypy), and unit tests (pytest with ≥60% coverage).
- **On merge to main:** Automatic full test suite, Docker image builds, and storage in registry.
- **On staging/production:** Manual triggers via server updates and make rebuild.

## 11. Troubleshooting & Edge Cases

### 11.1 Accidental Merge to Wrong Branch
If code is accidentally merged to production:
```bash
git checkout main
git revert <merge-commit-hash> -m 1
git push origin main
# Create issue to track the mistake and plan proper release
```

### 11.2 Out-of-Sync Branches
```bash
# Sync staging into production (if intentional)
git checkout staging && git pull origin staging
git checkout main && git pull origin main
git merge --no-ff staging -m "chore: sync staging to production"
git push origin main
# Then merge production back to main
git checkout development && git pull origin development
git merge --no-ff main
git push origin development
```

## 12. Team Responsibilities
- **Code Owner (Primary Developer):** Creates features/PRs, fixes CI, squashes and merges to main.
- **Reviewer:** Reviews within 48 hours, validates runtime behavior, signs off on staging promotion.
- **QA/Tester:** Validates in staging for 24+ hours, tests critical workflows, signs off for production.
- **Team Lead/Release Manager:** Authorizes staging/production steps, manages release schedule, handles incidents.

## 13. Change Log

| **Date** | **Change** | **Author** |
|---|---|---|
| 2026-07-09 | Initial branching strategy documentation | Team |

**Owner:** OpenScientist Development Team
**Last Updated:** 2026-07-13
**Status:** Active
