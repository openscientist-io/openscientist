# Vertex AI Provider Integration Design

**Date:** 2025-11-22
**Status:** Design Complete
**Issue:** [#8 Migrate to Vertex AI for model access](https://github.com/justaddcoffee/openscientist/issues/8)

## Overview

This design adds support for multiple AI model providers (CBORG, Vertex AI, AWS Bedrock) through a plugin-style provider abstraction layer. The current implementation hardcodes CBORG API access and cost tracking. This refactoring:

1. Abstracts provider-specific logic (auth, cost tracking, configuration)
2. Adds Vertex AI support via Google Cloud
3. Maintains backward compatibility with existing CBORG functionality
4. Sets foundation for future providers (Bedrock, etc.)

## Goals

- ✅ Support CBORG and Vertex AI as model providers
- ✅ Abstract provider-specific configuration and cost tracking
- ✅ Add application-level budget controls (provider-agnostic)
- ✅ Maintain existing CBORG functionality without breaking changes
- ✅ Provide clear documentation for each provider setup
- ✅ Track project-level costs (not per-job due to concurrency)

## Non-Goals

- ❌ Per-job cost tracking (impossible with concurrent jobs)
- ❌ Complete AWS Bedrock implementation (stub only for now)
- ❌ Real-time cost tracking for Vertex AI (GCP billing has inherent lag)
- ❌ Abstracting away Claude CLI dependency (keeping Claude-centric pattern)

## Architecture

### Provider Structure

```
src/openscientist/providers/
├── __init__.py       # Provider factory: get_provider()
├── base.py           # BaseProvider interface + CostInfo dataclass
├── cborg.py          # CborgProvider (migrated from cost_tracker.py)
├── vertex.py         # VertexProvider (new - GCP Billing API)
└── bedrock.py        # BedrockProvider (stub for future)
```

### Provider Selection

Environment variable `CLAUDE_PROVIDER` determines which provider to use:
- `cborg` (default) - CBORG API
- `vertex` - Google Cloud Vertex AI
- `bedrock` - AWS Bedrock (not yet implemented)

### Integration Points

```python
# In orchestrator.py
from openscientist.providers import get_provider

provider = get_provider()  # Loads based on CLAUDE_PROVIDER env var
provider.setup_environment()  # Configures Claude CLI env vars

# In web_app.py (cost dashboard)
cost_info = provider.get_cost_info(lookback_hours=24)
budget_check = provider.check_budget_limits()
```

## Provider Interface

### BaseProvider (Abstract)

```python
class BaseProvider(ABC):
    """Abstract base class for model providers."""

    def __init__(self):
        """Validate configuration on initialization."""
        errors = self._validate_required_config()
        if errors:
            raise ValueError(f"{self.name} configuration errors:\n" +
                           "\n".join(f"  - {err}" for err in errors))

        warnings = self._validate_optional_config()
        if warnings:
            logger.warning(f"{self.name} configuration warnings:\n" +
                         "\n".join(f"  - {warn}" for warn in warnings))

    @abstractmethod
    def _validate_required_config(self) -> List[str]:
        """Validate required config. Returns list of errors."""
        pass

    def _validate_optional_config(self) -> List[str]:
        """Validate optional config. Returns list of warnings."""
        return []

    @abstractmethod
    def setup_environment(self) -> None:
        """Configure environment variables for Claude CLI."""
        pass

    @abstractmethod
    def get_cost_info(self, lookback_hours: int = 24) -> CostInfo:
        """Get project spending information.

        Args:
            lookback_hours: Time window for recent_spend_usd

        Returns:
            CostInfo with total and recent spend
        """
        pass

    def check_budget_limits(self, lookback_hours: int = 24) -> Dict[str, Any]:
        """Check if budget limits are exceeded.

        Returns:
            {
                "can_proceed": bool,
                "warnings": List[str],
                "errors": List[str]
            }
        """
        # Implementation in base class (uses get_cost_info)

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider name for logging/display."""
        pass
```

### CostInfo Dataclass

```python
@dataclass
class CostInfo:
    """Provider-agnostic cost information."""

    provider_name: str

    # Total project spending (all time)
    total_spend_usd: float

    # Recent spending (configurable time window)
    recent_spend_usd: float
    recent_period_hours: int  # e.g., 24 for "last 24h"

    # Budget tracking (optional - provider-specific)
    budget_limit_usd: Optional[float] = None
    budget_remaining_usd: Optional[float] = None

    # Data freshness
    last_updated: datetime
    data_lag_note: Optional[str] = None  # e.g., "Data current as of 6:35 AM ET"

    # Provider-specific extras
    key_expires: Optional[str] = None  # CBORG only
    metadata: Dict[str, Any] = field(default_factory=dict)
```

## Provider Implementations

### CborgProvider

Migrates existing `cost_tracker.py` logic.

**Configuration (required):**
- `ANTHROPIC_AUTH_TOKEN` - CBORG API key
- `ANTHROPIC_BASE_URL` - https://api.cborg.lbl.gov

**Configuration (optional):**
- `ANTHROPIC_MODEL` - Model identifier (defaults to Claude CLI default)

**Cost tracking:**
- Total spend: `/key/info` endpoint → `info.spend`
- Recent spend: `/user/daily/activity` endpoint → sum costs over time window
- Budget limit: `/key/info` endpoint → `info.max_budget`

**Environment setup:**
- Disables Vertex AI vars if accidentally set
- Sets `ANTHROPIC_API_KEY` from `ANTHROPIC_AUTH_TOKEN` (for Claude CLI)

### VertexProvider

Uses Google Cloud Vertex AI with GCP Billing API for cost tracking.

**Configuration (required):**
- `ANTHROPIC_VERTEX_PROJECT_ID` - GCP project ID
- `GOOGLE_APPLICATION_CREDENTIALS` - Path to service account JSON
- `GCP_BILLING_ACCOUNT_ID` - Billing account ID (for cost tracking)
- `CLOUD_ML_REGION` - Region (e.g., us-east5)

**Configuration (optional):**
- `ANTHROPIC_MODEL` - Defaults to claude-sonnet-4-5@20250929
- `ANTHROPIC_SMALL_FAST_MODEL` - Defaults to claude-haiku-4-5@20251001
- `VERTEX_REGION_CLAUDE_4_5_SONNET` - Region override for Sonnet
- `VERTEX_REGION_CLAUDE_4_5_HAIKU` - Region override for Haiku
- `DISABLE_PROMPT_CACHING` - Set to 1 to disable

**Prerequisites:**
1. **BigQuery billing export enabled** in GCP Console (Billing → Billing Export)
2. **Service account permissions:**
   - `BigQuery Data Viewer` (for cost queries)
   - `Billing Account Viewer` (for billing access)
   - `Vertex AI User` (for model access)

**Cost tracking:**
- Uses BigQuery to query billing export table
- Total spend: SUM(cost) WHERE service = 'Vertex AI'
- Recent spend: Same with time filter
- **Has 1-6 hour data lag** (GCP billing delay)

**Environment setup:**
- Sets `CLAUDE_CODE_USE_VERTEX=1` (enables Vertex mode in Claude CLI)
- Clears CBORG settings if present
- Claude CLI constructs Vertex AI URLs automatically from project ID + region

### BedrockProvider (Stub)

Placeholder for future AWS Bedrock support.

**Status:** Not implemented - raises `NotImplementedError`

**Documentation:** Marked as "Coming Soon" in README and .env.example

## Budget Controls

### Application-Level Limits

New environment variables (apply to all providers):

```bash
MAX_PROJECT_SPEND_TOTAL_USD=1000.00     # Hard limit: total spend
MAX_PROJECT_SPEND_24H_USD=100.00        # Hard limit: 24h spend
WARN_PROJECT_SPEND_24H_USD=50.00        # Warning threshold: 24h spend
```

### Budget Enforcement

**Before job start** (in `job_manager.py`):
```python
provider = get_provider()
budget_check = provider.check_budget_limits()

if not budget_check["can_proceed"]:
    # Fail job with budget error
    error_msg = "Budget exceeded:\n" + "\n".join(budget_check["errors"])
    self._update_job_status(job_id, JobStatus.FAILED, error=error_msg)
    return

if budget_check["warnings"]:
    # Log warnings but proceed
    for warning in budget_check["warnings"]:
        logger.warning(warning)
```

**Budget check logic:**
1. Check total spend vs `MAX_PROJECT_SPEND_TOTAL_USD`
2. Check 24h spend vs `MAX_PROJECT_SPEND_24H_USD`
3. Check 24h spend vs `WARN_PROJECT_SPEND_24H_USD` (warning only)
4. Check provider budget if available (CBORG only: `max_budget` field)

## Configuration Examples

### CBORG (.env)

```bash
CLAUDE_PROVIDER=cborg
ANTHROPIC_AUTH_TOKEN=sk-ant-your-token-here
ANTHROPIC_BASE_URL=https://api.cborg.lbl.gov
ANTHROPIC_MODEL=anthropic/claude-sonnet

# Budget controls
MAX_PROJECT_SPEND_TOTAL_USD=1000.00
MAX_PROJECT_SPEND_24H_USD=100.00
WARN_PROJECT_SPEND_24H_USD=50.00
```

### Vertex AI (.env)

```bash
CLAUDE_PROVIDER=vertex
CLAUDE_CODE_USE_VERTEX=1
CLOUD_ML_REGION=us-east5
ANTHROPIC_VERTEX_PROJECT_ID=test-project-covid-19-277821
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
GCP_BILLING_ACCOUNT_ID=015426-0B5674-83F27C
ANTHROPIC_MODEL=claude-sonnet-4-5@20250929
ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5@20251001
VERTEX_REGION_CLAUDE_4_5_SONNET=us-east5
VERTEX_REGION_CLAUDE_4_5_HAIKU=us-east5

# Budget controls
MAX_PROJECT_SPEND_TOTAL_USD=1000.00
MAX_PROJECT_SPEND_24H_USD=100.00
WARN_PROJECT_SPEND_24H_USD=50.00
```

## Code Changes

### Files to Create

- `src/openscientist/providers/__init__.py` - Provider factory
- `src/openscientist/providers/base.py` - BaseProvider + CostInfo
- `src/openscientist/providers/cborg.py` - CborgProvider implementation
- `src/openscientist/providers/vertex.py` - VertexProvider implementation
- `src/openscientist/providers/bedrock.py` - BedrockProvider stub
- `docs/VERTEX_SETUP.md` - Vertex AI setup guide

### Files to Modify

**`src/openscientist/orchestrator.py`:**
- Remove `cost_tracker` imports
- Remove lines 33-36 (Vertex disable code)
- Add `from .providers import get_provider`
- Call `provider.setup_environment()` at start of `run_discovery()`
- Remove per-job cost tracking (lines 451-456, 464, 478, 516-517)

**`src/openscientist/job_manager.py`:**
- Remove `cost_usd` field from `JobInfo` dataclass (line 46)
- Remove cost loading in `_load_job_info()` (line 431)
- Remove `cost_usd` from JobInfo creation (line 457)
- Update `summary()` to use provider cost info instead of job costs (line 395)
- Add budget check in `start_job()` before starting jobs

**`src/openscientist/web_app.py`:**
- Add project cost dashboard to `index()` page
- Remove per-job cost displays (lines 264, 377)
- Update summary cost display (line 295) to use provider cost info
- Display budget warnings/errors if present

**`src/openscientist/cost_tracker.py`:**
- **Deprecate** - Add deprecation notice at top
- Keep file temporarily for backward compatibility (can delete later)

**`README.md`:**
- Add "Model Provider Configuration" section
- Document CBORG, Vertex AI, Bedrock (coming soon)

**`.env.example`:**
- Add `CLAUDE_PROVIDER` selection
- Add all provider configs with inline comments
- Add budget control variables

### Files to Delete (later)

- `src/openscientist/cost_tracker.py` - After confirming providers work

## UI Changes

### Project Cost Dashboard (New)

Add to main page before job list:

```
┌──────────────────────────────────────────────────────────┐
│ Project Costs                                            │
├──────────────────────────────────────────────────────────┤
│  $156.42        $12.50         $43.58      Provider:     │
│  Total Spend    Last 24h       Budget      Vertex AI     │
│                                Remaining   (as of 6:35am)│
│                                                           │
│  ⚠️ Warning: Last 24h spend approaching limit ($50)      │
└──────────────────────────────────────────────────────────┘
```

### Job List Changes

**Remove:**
- Cost column from jobs table

**Keep:**
- Job ID, status, research question, iterations, findings, created time

### Job Detail Page Changes

**Remove:**
- Per-job cost display

**Keep:**
- All other job information

## Testing Plan

### CBORG Provider Testing

**Test 1: API endpoints**
```bash
# Test /key/info
curl https://api.cborg.lbl.gov/key/info \
  --header "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN"

# Test /user/daily/activity
curl "https://api.cborg.lbl.gov/user/daily/activity?..." \
  -H "x-litellm-api-key: $ANTHROPIC_AUTH_TOKEN"
```

**Test 2: Provider initialization**
```python
from openscientist.providers import get_provider
provider = get_provider()  # Should load CBORG
cost_info = provider.get_cost_info(lookback_hours=24)
print(f"Total: ${cost_info.total_spend_usd:.2f}")
print(f"Last 24h: ${cost_info.recent_spend_usd:.2f}")
```

**Test 3: Claude CLI integration**
```bash
export CLAUDE_PROVIDER=cborg
claude -p "Test message" --output-format text
```

### Vertex AI Provider Testing

**Prerequisites:**
- BigQuery billing export enabled
- Service account with proper roles
- Credentials file downloaded

**Test 1: Environment setup**
```bash
export CLAUDE_PROVIDER=vertex
# ... set all Vertex env vars ...
```

**Test 2: Provider initialization**
```python
from openscientist.providers import get_provider
provider = get_provider()  # Should load VertexProvider
cost_info = provider.get_cost_info(lookback_hours=24)
print(f"Provider: {cost_info.provider_name}")
print(f"Data lag: {cost_info.data_lag_note}")
```

**Test 3: BigQuery verification**
```bash
# Verify billing export table exists
bq ls --project_id=$ANTHROPIC_VERTEX_PROJECT_ID billing_export
```

**Test 4: Claude CLI with Vertex**
```bash
claude -p "Test Vertex AI" --output-format text
```

### Budget Controls Testing

**Test 1: Budget exceeded**
```bash
# Set very low limit
export MAX_PROJECT_SPEND_TOTAL_USD=0.01

# Try to start job - should fail with budget error
```

**Test 2: Warning threshold**
```bash
# Set warning threshold below current spend
export WARN_PROJECT_SPEND_24H_USD=1.00

# Should see warning in logs but job proceeds
```

## Documentation

### README.md Updates

Add provider configuration section with:
- Overview of available providers
- Quick start for each provider
- Links to detailed setup guides

### New: docs/VERTEX_SETUP.md

Comprehensive Vertex AI setup guide:
1. Enable required GCP APIs
2. Enable BigQuery billing export
3. Create service account with proper roles
4. Configure environment variables
5. Verify setup
6. Troubleshooting common issues

### Updated: .env.example

Add:
- Provider selection (`CLAUDE_PROVIDER`)
- All provider configurations (commented out with descriptions)
- Budget control variables
- Inline comments explaining each variable

## Migration Path

### Phase 1: Create Provider Infrastructure
1. Create `providers/` module with base classes
2. Implement `CborgProvider` (migrate from `cost_tracker.py`)
3. Add tests for provider factory and CBORG provider

### Phase 2: Add Vertex AI Support
1. Implement `VertexProvider` with GCP Billing API
2. Create `docs/VERTEX_SETUP.md`
3. Test Vertex AI integration end-to-end

### Phase 3: Update Orchestrator & UI
1. Remove per-job cost tracking from orchestrator
2. Update job_manager to use providers
3. Add project cost dashboard to web UI
4. Remove per-job cost displays

### Phase 4: Budget Controls
1. Add application-level budget limits
2. Implement budget checking in job start
3. Add budget warnings to UI

### Phase 5: Documentation & Cleanup
1. Update README.md with provider docs
2. Update .env.example
3. Deprecate cost_tracker.py (keep for now)
4. Add Bedrock stub for future

## Future Enhancements

- **AWS Bedrock support** - Complete BedrockProvider implementation
- **Provider-specific optimizations** - Different models, regions, etc.
- **Cost forecasting** - Predict job costs before running
- **Detailed cost analytics** - Track costs by model, time period, etc.
- **Multi-provider jobs** - Use different providers for different tasks
- **Cost alerting** - Email/Slack notifications when budget thresholds hit

## Open Questions

None - design is complete and ready for implementation.

## References

- Issue: [#8 Migrate to Vertex AI](https://github.com/justaddcoffee/openscientist/issues/8)
- CBORG API: https://api.cborg.lbl.gov/docs
- GCP Billing API: https://cloud.google.com/billing/docs
- Vertex AI Pricing: https://cloud.google.com/vertex-ai/pricing
- Claude Code CLI: Uses built-in Vertex AI support when `CLAUDE_CODE_USE_VERTEX=1`
