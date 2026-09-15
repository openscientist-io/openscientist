# OpenScientist Documentation

Index of the documentation in this repository. Everything the team maintains
lives here as markdown so it is versioned alongside the code it describes.

## Process and workflow

| Document | Covers |
|---|---|
| [BRANCHING.md](BRANCHING.md) | Branch structure, promotion rules to staging and production, hotfixes, branch protection, release management, rollback |
| [CODING_GUIDELINES.md](CODING_GUIDELINES.md) | Code standards, testing expectations, architecture principles, development workflow, security, performance, refactoring |
| [code-review-governance.md](code-review-governance.md) | Mandatory peer review, PR and issue templates, contribution rules, repository governance |
| [CICD.md](CICD.md) | Pre-commit hooks, continuous integration gates, deployment pipeline, dependency automation |

## Running and operating

| Document | Covers |
|---|---|
| [DEPLOYMENT.md](DEPLOYMENT.md) | How to deploy the application |
| [ENVIRONMENTS.md](ENVIRONMENTS.md) | Environment matrix, promotion model, configuration and secrets separation, migrations across environments |
| [MAINTENANCE.md](MAINTENANCE.md) | Operational runbook: dependencies, backups, key rotation, container hygiene, diagnostics, rollback |
| [MAINTENANCE_RECOMMENDATIONS.md](MAINTENANCE_RECOMMENDATIONS.md) | Forward-looking advice on keeping the system healthy: dependency cadence, base-image rebuilds, provider deprecation, documentation currency |
| [ACR_IMAGES.md](ACR_IMAGES.md) | Prebuilt container images in Azure Container Registry |

## Quality and security

| Document | Covers |
|---|---|
| [QA.md](QA.md) | Test suite architecture, unit and integration testing, acceptance validation, CI gates |
| [SECURITY_REVIEW.md](SECURITY_REVIEW.md) | Security review findings and posture |

## Architecture and reference

| Document | Covers |
|---|---|
| [DESIGN.md](DESIGN.md) | System design of the hypothesis agent |
| [DISCOVERY_AGENT_REFERENCE.md](DISCOVERY_AGENT_REFERENCE.md) | Discovery agent reference |
| [AGENT_ABSTRACTION_DEPLOYMENT.md](AGENT_ABSTRACTION_DEPLOYMENT.md) | Record of the agent abstraction refactor and its migration |

Also see [../CONTRIBUTING.md](../CONTRIBUTING.md) for environment setup and local
development, and [../CLAUDE.md](../CLAUDE.md) for agent-facing repository context.

## Conventions

- Documentation is markdown in this folder, versioned with the code.
- Where a document was previously maintained on the shared drive, the copy here
  is the canonical one. Update it here rather than editing the drive copy.
- Keep point-in-time artefacts (audit reports, acceptance records, milestone
  sign-offs) out of this repository. Those are records rather than
  documentation, and some carry confidentiality terms that a public repository
  cannot satisfy.
