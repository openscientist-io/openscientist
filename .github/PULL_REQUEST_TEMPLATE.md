<!--
Thanks for contributing to OpenScientist! Please fill out this template completely.
See ../docs/code-review-governance.md for the full review process.
-->

## Description
<!-- What does this PR do, and why? -->

## Related Issue
Closes #

## Type of Change
- [ ] New feature (`feat/`)
- [ ] Bug fix (`fix/`)
- [ ] Refactor (`refactor/`)
- [ ] Documentation (`docs/`)
- [ ] Test (`test/`)
- [ ] Hotfix (`hotfix/`)

## Test Plan
<!-- How did you verify this change? Include commands run and results. -->

## Self-Review Checklist
- [ ] Code follows the [contributing guidelines](../CONTRIBUTING.md) and [code review governance](../docs/code-review-governance.md)
- [ ] Tests added/updated; coverage stays at or above the 75% floor and does not drop more than 0.5 points below `main`
- [ ] `ruff check`, `ruff format`, and `mypy` pass locally
- [ ] No secrets, credentials, or environment-specific data included
- [ ] Documentation updated if behavior changed
- [ ] Breaking changes called out below (if any)

## Breaking Changes
<!-- None / describe here -->

---
### For Promotion PRs Only (`development → staging`, `staging → main`)
<!-- `main` is the production deployment branch: merging to `main` deploys production. -->

- [ ] Risk assessment included
- [ ] Rollback plan documented (for major changes)
- [ ] Staging validation checklist complete and linked (production PRs only)
- [ ] QA sign-off obtained (production PRs only)
