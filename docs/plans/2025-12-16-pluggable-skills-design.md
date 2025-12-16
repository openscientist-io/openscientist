# Pluggable Skills Design

**Date:** 2025-12-16
**Status:** Draft
**Issue:** https://github.com/justaddcoffee/shandy/issues/9

## Overview

Create a separate repository (`open-science-skills`) for community-contributed domain skills that can be shared across SHANDY and other AI tools using Claude Code's native skill discovery.

## Goals

1. **Scale domain expertise** - Community can contribute specialized knowledge
2. **Quality/consensus building** - Build best practices for each scientific domain
3. **Discoverability** - Users can find relevant skills for their data
4. **Customization** - Labs/teams can extend with their own workflows

## Design

### Repository: `open-science-skills`

A separate Git repository containing domain-specific skills for scientific discovery.

**Structure:**
```
open-science-skills/
├── metabolomics/
│   └── SKILL.md
├── genomics/
│   └── SKILL.md
├── proteomics/
│   └── SKILL.md
├── structural-biology/
│   ├── SKILL.md
│   └── references/
│       ├── alphafold-confidence.md
│       └── validation-metrics.md
├── README.md
└── CONTRIBUTING.md
```

Each domain is a top-level directory containing at minimum a `SKILL.md` file.

### Skill Format

Standard Claude Code skill format with YAML frontmatter:

```yaml
---
name: metabolomics
description: Metabolomics-specific analysis strategies including pathway analysis, flux calculations, and metabolite interpretation
---

# Metabolomics Analysis

[Skill content...]
```

**Requirements:**
- `name`: lowercase, hyphens only, max 64 chars
- `description`: max 1024 chars, explains what it does AND when to use it
- Content: under 5,000 words (split into reference files if larger)

### Installation

Skills are cloned/copied into `.claude/skills/` at the project level.

**After installation:**
```
.claude/skills/
├── metabolomics/
│   └── SKILL.md
├── genomics/
│   └── SKILL.md
└── ...
```

**Installation options:**
1. Part of `make setup` (automatic)
2. Separate `make install-skills` (optional)
3. Manual clone with instructions in README

Installation must be non-destructive - never overwrite existing skills.

### What Stays in SHANDY

Workflow skills are SHANDY-specific and remain in the SHANDY repo:

```
shandy/.claude/skills/
├── workflow/
│   ├── hypothesis-generation/
│   ├── result-interpretation/
│   ├── prioritization/
│   └── stopping-criteria/
```

These are not part of `shandy-openskills`.

### Contribution Model

**Initial approach:** Open PRs
- Anyone can submit a PR to add or improve a domain skill
- Maintainers review and merge

**Future governance (when community grows):**
- Domain owners responsible for reviewing their domain
- Tiered trust: core skills (well-tested) vs community skills (less vetted)
- RFC process for new domains

### How It Works with Claude Code

Claude Code automatically discovers skills in `.claude/skills/`:
1. At startup, loads `name` and `description` from all SKILL.md files
2. Based on user's request, Claude decides which skills are relevant
3. Claude reads the full SKILL.md content only when needed
4. Progressive disclosure keeps context usage efficient

Nested directories are supported (verified by testing with existing superpowers skills).

## Future Enhancements

**Runtime skill selection:**
- Let users select which domains to use per job
- Could be job config, UI selection, or symlink-based enabling

**Quality mechanisms:**
- Skill templates and linting
- Testing framework for skills
- Usage analytics to identify popular/effective skills

**Consensus building:**
- Discussion threads per domain
- Voting or review system for controversial approaches

## Implementation Steps

1. Create `shandy-openskills` repository on GitHub
2. Migrate existing domain skills from SHANDY
3. Add README and CONTRIBUTING guide
4. Update SHANDY's `make setup` or add `make install-skills`
5. Update SHANDY docs to reference the new repo

## Open Questions

- Should we support skill dependencies (one skill referencing another)?
- How to handle domain-specific Python code/scripts bundled with skills?
- Versioning strategy: tags per release, or rely on main branch?

## References

- [Claude Code Skills Documentation](https://code.claude.com/docs/en/skills)
- [Agent Skills Overview](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview)
- [Anthropic Skills Repo](https://github.com/anthropics/skills)
