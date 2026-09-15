# OpenScientist Coding Guidelines and Development Workflow

> Source: internal documentation, converted from the shared drive. Keep this file as the canonical version.

## Overview
This document defines standardized coding conventions, development practices, and workflow processes for OpenScientist contributors. All contributors must follow these guidelines to maintain code quality, consistency, and reliability across the project.
- **Scope:** These guidelines apply to all Python code, tests, documentation, and contributions to the OpenScientist repository.

## 1. Code Standards

### 1.1 Language and Version
- Python: 3.12 or later (strict requirement)
- Type hints: Mandatory for all public functions and classes
- All type checking must pass mypy --strict

### 1.2 Formatting and Style
Automatic formatting is mandatory:
# Format all code
uv run ruff format src/ tests/
# Lint check
uv run ruff check src/ tests/
- Use Ruff for all linting and formatting
- Line length: 88 characters (Ruff default)
- String style: Double quotes (Ruff default)
- Import ordering: Handled automatically by Ruff
Before committing: Always run both formatters locally.

### 1.3 Type Checking
All code must pass strict mypy checks:
uv run mypy src/openscientist/ tests/
Rules:
- All function parameters and return types must be annotated
- No Any types (request explicit mypy exemptions in code review if truly needed)
- Use Optional[T] instead of T | None in type hints (for compatibility)
- Async functions must explicitly return types
Example:
async def process_job(job_id: str) -> dict[str, str]:
"""Process a single job by ID."""
job = await db.get_job(job_id)
if job is None:
raise ValueError(f"Job not found: {job_id}")
return {"status": job.status}

### 1.4 Comments and Documentation
**Default: No comments.** Only add comments when the WHY is non-obvious:
- Hidden constraints (e.g., "must call this before X due to state dependency")
- Subtle invariants (e.g., "this dict key ordering matters for the SQL query")
- Workarounds for specific bugs or limitations
- Non-obvious behavior that would surprise a reader
Bad comment (explains WHAT the code does - remove it):
# Add one to the count
count += 1
Good comment:
# Increment to match 1-indexed job numbers used in user-facing UI
count += 1
Docstrings: One-line only for internal functions:
def _calculate_hash(data: bytes) -> str:
"""Compute SHA256 hash of binary data."""
return hashlib.sha256(data).hexdigest()
Public APIs: Brief description, no implementation details.

### 1.5 Naming Conventions
- Classes: PascalCase (JobExecutor, AsyncDatabase)
- Functions/methods: snake_case (process_job, get_status)
- Constants: UPPER_SNAKE_CASE (MAX_RETRIES, DEFAULT_TIMEOUT)
- Private: Leading underscore (_internal_helper, _cache)
- Booleans: Start with verb when possible (is_ready, has_error, can_execute)

## 2. Testing

### 2.1 Testing Philosophy
Use real database objects, not mocks. Tests use testcontainers to spin up real PostgreSQL databases. Always create real SQLAlchemy model instances.
Good:
@pytest_asyncio.fixture
async def test_job(db_session: AsyncSession, webapp_user: User) -> Job:
job = Job(
owner_id=webapp_user.id,
title="Test research question",
status="pending",
)
db_session.add(job)
await db_session.commit()
return job
Bad (Don't create mocks):
class MockJob:
job_id = "fake-id"
status = "pending"

### 2.2 Test Organization
Organize tests to mirror source structure:
tests/
├── webapp/
│   ├── pages/
│   │   ├── test_jobs_list.py
│   │   ├── test_job_detail.py
│   │   └── test_new_job.py
│   └── conftest.py
├── api/
│   ├── test_endpoints.py
│   └── conftest.py
├── job/
│   └── test_lifecycle.py
└── conftest.py  # Shared fixtures

### 2.3 Test Requirements
- Minimum coverage: 60% overall
- Run tests before committing: uv run pytest
- Run with coverage: uv run pytest --cov=src/openscientist --cov-report=term-missing
- All PRs must pass CI tests (automated)

### 2.4 UI Testing (NiceGUI)
Use user_simulation for browser-based tests. Variable is browser (not user, to avoid confusion with database User model):
async with user_simulation(root=some_page) as browser:
browser.http_client.cookies.set("session_token", session_token)
await browser.open("/page")
await browser.should_see("Expected content")

## 3. Architecture and Design Principles

### 3.1 Component Isolation
- Each module has a single responsibility
- Keep imports minimal and explicit
- Avoid circular dependencies
- Use dependency injection for testability

### 3.2 Database Patterns
- Always use SQLAlchemy ORM (not raw SQL)
- Use async/await throughout (AsyncSession)
- Transactions are handled at the service layer
- Migrations use Alembic (src/openscientist/database/migrations/)

### 3.3 Async/Await
All I/O operations must be async:
Good:
async def fetch_job(job_id: str) -> Job:
job = await db.get_job(job_id)
return job
Bad (Blocks event loop):
def fetch_job(job_id: str) -> Job:
job = db.get_job(job_id)
return job

### 3.4 Error Handling
At system boundaries (user input, external APIs), validate and handle errors explicitly:
try:
validated_input = validate_research_question(user_input)
except ValueError as e:
return {"error": str(e), "status": 400}
Internal code: Trust framework guarantees. Don't add try/except for cases that cannot happen:
Good (trust that job.status is always valid as enforced by enum):
if job.status == JobStatus.PENDING:
await execute_job(job)
Bad (redundant error handling):

try:
if job.status == JobStatus.PENDING:
await execute_job(job)
except ValueError:
# This can never happen
Pass

### 3.5 UI Component Reuse
Component reuse is critical. All UI elements with similar functionality MUST use shared components from src/openscientist/webapp_components/ui_components.py.
Always use error banners instead of creating inline errors:
from openscientist.webapp_components.ui_components import (
render_config_error_banner,  # Provider config errors
render_alert_banner,         # Generic error/warning/info
)
# Provider configuration error (e.g., missing API key)
render_config_error_banner(provider_name, config_errors, show_back_button=False)
# Generic alert (severity: "error", "warning", "info")
render_alert_banner(
title="Something went wrong",
message="Detailed explanation here",
severity="error",
details=["Detail 1", "Detail 2"],
)
Use status badges get_status_badge_props() and render_status_cell_slot() for job status:
from openscientist.webapp_components.ui_components import (
get_status_badge_props,
render_status_cell_slot,
)
badge_props = get_status_badge_props(job.status)
render_status_cell_slot(badge_props)
When adding a new UI pattern used in multiple places:
- Add reusable function to ui_components.py
- Document in CODING_GUIDELINES.md
- Update existing code to use the component

## 4. Development Workflow

### 4.1 Branch Creation
Branches are created from main. Use one of these naming patterns:
- fix/ : Bug fixes. Example: fix/job-status-display
- feat/ : New features. Example: feat/cost-tracking-api
- refactor/ : Code refactoring. Example: refactor/extract-job-manager
- docs/ : Documentation. Example: docs/add-deployment-guide
- test/ : Test additions/fixes. Example: test/job-lifecycle-coverage
Branch names must use lowercase, use hyphens (not underscores or spaces), and be descriptive.

### 4.2 Commit Messages
Commits follow Conventional Commits (conventionalcommits.org):
Format:
<type>(<scope>): <subject>
<body>
Co-Authored-By: Name <email>
Types (required):
- feat: New feature
- fix: Bug fix
- refactor: Code refactoring (no behavior change)
- test: Test additions or fixes
- docs: Documentation
- chore: Build, CI, dependencies, tooling
Scope (optional): Component affected (e.g., api, webapp, job-manager)
Subject (required):
- Lowercase, no period
- Imperative mood ("add" not "adds" or "added")
- Brief (50 chars max)
Body (optional):
- Detailed explanation of why (not what)
- Wrap at 72 characters
- Reference issue numbers: Fixes #123
Examples:
fix(job): guard consensus regeneration against stale answer
When regenerating consensus after a timeout, ensure the last successful response is not older than the timeout threshold. Fixes #234
feat(api): add budget tracking endpoint
Expose current spend and budget remaining via GET /api/budget
with per-project and 24h window metrics.

### 4.3 Creating a Pull Request
Before opening:
- Ensure your branch is up to date: git fetch origin && git rebase origin/main
- Run all checks locally:
uv run ruff format src/ tests/
uv run ruff check src/ tests/
uv run mypy src/openscientist/ tests/
uv run pytest
- Push your branch: git push origin
PR Title and Description:
```text
Title (under 70 chars):
fix(job): guard consensus regeneration against stale answer
Body:
## Summary
- Fixes stale consensus answers in regeneration flow
- Adds timestamp check before accepting cached responses
- Includes integration test for edge case
## Test Plan
- Run existing job lifecycle tests
- Manually test regenerate with timeout scenario
- Verify consensus timestamp validation in logs
```
Link your PR to an issue (if applicable).

**4.4 Code Review Process**
For authors:
- All PRs require at least one approval before merging
- Address feedback promptly (within 24 hours)
- Respond to all comments before requesting re-review
- Don't resolve conversations, let reviewers do it
For reviewers:
- Review within 48 hours
- Check: correctness, tests, style compliance, architecture fit
- Run the code locally if the change affects runtime behavior
- Comment inline for specific issues
- Approve once satisfied
CI Requirements (must pass before merge):
- Linting: ruff check
- Type checking: mypy
- Tests: pytest with coverage

## 5. Security and Secrets

### 5.1 Secrets Management
Never commit secrets, including:
- API keys, tokens, passwords
- Database credentials
- Private URLs or IPs
- AWS keys, Azure tokens
- Encryption keys
If you accidentally commit secrets:
- Revoke the secret immediately
- Use git log to find the commit
- Contact the team lead before attempting to remove history
- Plan a rotation/regeneration
Safe practices:
- Use .env files (in .gitignore)
- Use environment variables in CI/CD
- Reference CONTRIBUTING.md for local setup
- Use secrets scanning tools (GitHub Secret Scanning is enabled)

### 5.2 Credential Testing
- Only test with non-production credentials
- Never commit test API keys
- Use mock credentials in unit tests
- Integration tests use testcontainers (real database)

## 6. Performance and Efficiency

### 6.1 Database Queries
- Use JOIN instead of N+1 queries
- Specify only required columns with .select(Model.column1, Model.column2)
- Use indexes for frequently filtered columns
- Profile slow queries with EXPLAIN
Bad (N+1 query in loop):
jobs = await db.get_all_jobs()
for job in jobs:
owner = await db.get_user(job.owner_id)
Good:
jobs = await db.query(Job).options(
joinedload(Job.owner)
).all()

### 6.2 Async Best Practices
- Don't block event loop with sync calls
- Use asyncio.gather() for parallel operations
- Set timeouts on long-running operations
- Avoid sleep loops, use asyncio.Event or tasks

### 6.3 Resource Cleanup
Always use context managers:
async with AsyncSession() as session:
job = await session.get(Job, job_id)
# Session auto-closes

## 7. Refactoring and Cleanup

### 7.1 When to Refactor
- Refactor should be in separate commits from bug fixes
- Include rationale in commit message
- Don't refactor and add features in the same PR
- Leave code cleaner than you found it (boy scout rule)

### 7.2 What NOT to Do
Don't introduce:
- Half-finished implementations or experimental code
- Dead code branches (use git history if needed)
- Feature flags for incomplete features
- Premature abstractions (wait for 3+ similar patterns)
- Comments for hypothetical future requirements

### 7.3 Deletion Guidelines
If something is truly unused:
- Verify with grep -r across the codebase
- Check git blame to understand its purpose
- Check recent PRs that might reference it
- Delete it completely (no commented-out code)

## 8. Documentation

### 8.1 Inline Documentation
- Well-named functions and variables are self-documenting
- Add comments only for non-obvious WHY
- Update docstrings if behavior changes
- Don't document obvious implementations

### 8.2 User-Facing Documentation
- Update README.md if adding user features
- Update CONTRIBUTING.md for workflow changes
- Add docstrings to public APIs
- Include examples in docs for complex features

### 8.3 Commits and PRs
- PR descriptions explain the problem and solution
- Commit messages capture the rationale
- Link to issues and related PRs
- Reference documentation in commit bodies if relevant

## 9. Local Development Checklist
Before pushing code:
# 1. Run formatter
uv run ruff format src/ tests/
# 2. Run linter
uv run ruff check src/ tests/
# 3. Run type checker
uv run mypy src/openscientist/ tests/
# 4. Run tests
uv run pytest --cov=src/openscientist
# 5. Verify coverage meets 60% minimum
# 6. Review your own diff before committing
git diff
# 7. Commit with proper message
git commit -m "type(scope): description"
# 8. Push to your branch
git push origin branch-name

## 10. Questions and Updates
If anything in these guidelines is unclear, causing friction, or needs updating, open an issue or PR on this document. Guidelines should evolve with team feedback.

## Appendix: Quick Reference
- Format: uv run ruff format src/ tests/ (Purpose: Auto-fix formatting)
- Lint: uv run ruff check src/ tests/ (Purpose: Check style issues)
- Type check: uv run mypy src/openscientist/ tests/ (Purpose: Verify type safety)
- Test: uv run pytest (Purpose: Run all tests)
- Test + Coverage: uv run pytest --cov=src/openscientist (Purpose: Tests with coverage)
- Dev server: uv run python -m openscientist.web_app --reload (Purpose: Start development server)
**Last updated:** 2026-07-09
**Owner:** OpenScientist Development Team
