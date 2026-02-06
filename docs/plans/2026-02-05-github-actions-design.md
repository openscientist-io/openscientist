# Add Standard GitHub Actions (Issue #90) - Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add CI via GitHub Actions and a justfile so every PR gets lint, format, typecheck, and test checks.

**Architecture:** New `justfile` for dev tasks (CI calls `just ci`). New `.github/workflows/main.yaml`. Existing Makefile stays for Docker/deploy. Fix existing lint/format/mypy issues so CI passes green on first merge.

**Tech Stack:** GitHub Actions, uv, just, ruff, mypy, pytest

---

### Task 1: Create the justfile

**Files:**
- Create: `justfile`

**Step 1: Write the justfile**

```just
# Default: show available recipes
default:
    @just --list

# Run the full test suite
test:
    uv run pytest

# Run linter
lint:
    uv run ruff check src/ tests/

# Check formatting (fails if not formatted)
format-check:
    uv run ruff format --check src/ tests/

# Auto-format code
format:
    uv run ruff format src/ tests/

# Run type checker
typecheck:
    uv run mypy src/

# Run all CI checks (lint and format first since they're fast)
ci: lint format-check typecheck test
```

**Step 2: Verify it works**

Run: `just --list`
Expected: Lists all recipes

**Step 3: Commit**

```bash
git add justfile
git commit -m "feat: add justfile with dev task recipes"
```

---

### Task 2: Fix ruff lint issues

**Files:**
- Modify: all files under `src/shandy/`

**Step 1: Auto-fix what ruff can fix**

Run: `uv run ruff check src/ tests/ --fix`

This handles: I001 (import sorting), F401 (unused imports), F541 (empty f-strings), F841 (unused variables), W291 (trailing whitespace), F811 (redefinition).

**Step 2: Manually fix remaining issues**

The auto-fix won't handle:
- `N818`: Exception names `TimeoutException` and `ForbiddenImportException` in `src/shandy/code_executor.py` - rename to `TimeoutError` and `ForbiddenImportError` (update all references)
- `N817`: `ElementTree as ET` in `src/shandy/literature.py` - add `# noqa: N817` (this is idiomatic Python)
- `N806`: `RESET_INTERVAL` in function - add `# noqa: N806` (constant-style name is intentional)

**Step 3: Verify clean**

Run: `just lint`
Expected: No errors

**Step 4: Commit**

```bash
git add -A
git commit -m "fix: resolve all ruff lint issues"
```

---

### Task 3: Fix ruff formatting

**Files:**
- Modify: 19 files under `src/shandy/`

**Step 1: Auto-format**

Run: `just format`

**Step 2: Verify clean**

Run: `just format-check`
Expected: No files would be reformatted

**Step 3: Commit**

```bash
git add -A
git commit -m "style: auto-format with ruff"
```

---

### Task 4: Fix mypy errors

**Files:**
- Modify: `pyproject.toml` (mypy config)
- Possibly modify: files with fixable type errors

There are 89 mypy errors, many from missing stubs (mcp, nicegui, bcrypt, dotenv, pandas). Strategy:

**Step 1: Add ignore_missing_imports for third-party libs without stubs**

Add to `pyproject.toml` under `[tool.mypy]`:

```toml
[[tool.mypy.overrides]]
module = [
    "mcp.*",
    "nicegui.*",
    "bcrypt.*",
    "dotenv.*",
    "fitz.*",
    "docx.*",
    "scanpy.*",
    "anndata.*",
    "seaborn.*",
]
ignore_missing_imports = true
```

**Step 2: Run mypy and assess remaining errors**

Run: `just typecheck`

Fix straightforward type errors (e.g., `None` assignments, `object` types). For complex ones in web_app.py / server.py, add targeted `# type: ignore[...]` comments with specific error codes.

**Step 3: Verify clean**

Run: `just typecheck`
Expected: `Success: no issues found` (or a small number of suppressed warnings)

**Step 4: Commit**

```bash
git add -A
git commit -m "fix: resolve mypy type errors and configure stub overrides"
```

---

### Task 5: Verify all tests still pass

**Step 1: Run full CI check**

Run: `just ci`
Expected: All four checks pass (lint, format-check, typecheck, test with 19 passing)

**Step 2: Commit if any fixes needed**

---

### Task 6: Add GitHub Actions workflow

**Files:**
- Create: `.github/workflows/main.yaml`

**Step 1: Create workflow file**

```yaml
name: Build and test

on:
  push:
    branches: [main]
  pull_request:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
        with:
          python-version: "3.12"
          enable-cache: true
          cache-dependency-glob: "uv.lock"
      - run: uv tool install rust-just
      - run: uv sync --dev
      - run: just ci
```

**Step 2: Commit**

```bash
git add .github/workflows/main.yaml
git commit -m "feat: add GitHub Actions CI workflow (closes #90)"
```

---

### Task 7: Push and create PR

**Step 1: Push branch**

Run: `git push -u origin feat/github-actions`

**Step 2: Create PR**

```bash
gh pr create --title "Add GitHub Actions CI and justfile" --body "..."
```

Reference issue #90 in the PR body.
