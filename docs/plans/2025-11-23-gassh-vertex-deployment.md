# SHANDY Deployment Plan: gassh → Vertex AI

**Date:** 2025-11-23
**Target:** shandy.alzassistant.org (gassh server)
**Goal:** Update production SHANDY to use Vertex AI instead of CBORG

## Current State

### Local Repository (main branch)
- Latest code with Vertex AI support
- Budget enforcer implemented and tested
- Service account: `claude-code-sa-2025-11-10@test-project-covid-19-277821.iam.gserviceaccount.com`
- Budget: $5,000/month with automatic key disabling at 100%

### Remote Server (gassh)
- Running older version of SHANDY
- Currently using CBORG
- URL: https://shandy.alzassistant.org
- Needs: Code update + Vertex AI configuration

### Shared Infrastructure (Option A)
- **Same GCP project** for both test and production: `test-project-covid-19-277821`
- **Same service account** for both environments
- **Shared $5,000/month budget** - if exceeded, both environments stop
- ⚠️ Testing and production share budget limits

## Phase 1: Commit and Push Local Changes

### Changes to Commit

**Modified:**
- `docs/VERTEX_SETUP.md` - Updated documentation
- `docs/RUN_LOCAL.md` - Deleted (shown as `D`)

**New files (untracked):**
- `vertex-ai-budget-enforcer/` - Budget enforcement Cloud Run service
  - `vertex-ai-budget-enforcer/main.py` - Fixed AttributeError bug (2025-11-23)
  - `vertex-ai-budget-enforcer/requirements.txt`
  - `vertex-ai-budget-enforcer/Dockerfile`
- `docs/VERTEX_BUDGET_SAFETY.md` - Budget safety documentation
- `notes/budget-enforcer-test-plan.md` - Test plan (validated)
- `.env.backup-test` - (DO NOT COMMIT - contains secrets)

### Commit Strategy

```bash
# 1. Create feature branch
git checkout -b vertex-budget-enforcer

# 2. Stage budget enforcer (production-ready, tested)
git add vertex-ai-budget-enforcer/

# 3. Stage documentation
git add docs/VERTEX_BUDGET_SAFETY.md
git add docs/VERTEX_SETUP.md
git add -u docs/RUN_LOCAL.md  # Stage deletion

# 4. Stage test plan
git add notes/budget-enforcer-test-plan.md

# 5. Stage deployment plan
git add docs/plans/2025-11-23-gassh-vertex-deployment.md

# 6. Verify .env files are NOT staged (check .gitignore)
git status | grep -E "\.env"

# 7. Commit
git commit -m "Add Vertex AI budget enforcer with automatic key disabling

- Implement Cloud Run service to disable SA keys when budget exceeded
- Fix AttributeError: use correct enum path for KeyType
- Deploy as budget-enforcer-00003-cp5 (verified working)
- Add comprehensive test plan (all phases passed)
- Document budget safety architecture and recovery procedures
- Add detailed deployment plan for gassh

Tested with:
- Budget threshold trigger (cost >= budget)
- Key disabling (USER_MANAGED keys only)
- Key re-enabling (manual recovery)
- Production-ready for gassh deployment"

# 8. Push feature branch to GitHub
git push origin vertex-budget-enforcer

# 9. Create Pull Request on GitHub
# - Go to: https://github.com/your-repo/shandy/pulls
# - Click "New Pull Request"
# - Base: main <- Compare: vertex-budget-enforcer
# - Review changes, add description, create PR
# - Merge PR after review

# 10. After PR merged, pull to local main
git checkout main
git pull origin main
```

### Pre-Push Checklist

- [ ] `.env` is in `.gitignore` (verify no secrets committed)
- [ ] `.env.backup-test` is NOT staged
- [ ] Service account JSON keys NOT committed
- [ ] All budget-enforcer files included
- [ ] Test plan included for future reference

## Phase 2: Prepare gassh Deployment

### 2.1: Transfer Service Account Key to gassh

The service account JSON key must be securely copied to gassh:

```bash
# On local machine:
# Copy service account key to gassh
scp /Users/jtr4v/vertexai-project-covid-19-277821-b9a24f9376ca.json \
    gassh:/path/to/shandy/.credentials/

# SSH to gassh
ssh gassh
```

**Security:**
- Use `scp` or secure file transfer
- Set permissions: `chmod 600 .credentials/*.json`
- Never commit to git
- Document location in deployment notes

### 2.2: Update .env on gassh

On gassh server, update SHANDY's `.env` file:

```bash
# On gassh:
cd /path/to/shandy

# Backup current .env
cp .env .env.backup-cborg-$(date +%Y%m%d)

# Edit .env
nano .env
```

**Changes needed:**

```bash
#############################################
# PROVIDER CONFIGURATION
#############################################

# Switch from CBORG to Vertex AI
CLAUDE_PROVIDER=vertex

# Comment out CBORG config
# ANTHROPIC_AUTH_TOKEN=sk-...
# ANTHROPIC_BASE_URL=https://api.cborg.lbl.gov
# ANTHROPIC_MODEL=anthropic/claude-sonnet
# ANTHROPIC_SMALL_FAST_MODEL=anthropic/claude-haiku

# Add Vertex AI config
ANTHROPIC_VERTEX_PROJECT_ID=test-project-covid-19-277821
GOOGLE_APPLICATION_CREDENTIALS=/path/to/shandy/.credentials/vertexai-project-covid-19-277821-b9a24f9376ca.json
CLOUD_ML_REGION=us-east5
VERTEX_REGION_CLAUDE_4_5_SONNET=us-east5
VERTEX_REGION_CLAUDE_4_5_HAIKU=us-east5
ANTHROPIC_MODEL=claude-sonnet-4-5@20250929
ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5@20251001

# Billing (optional - for cost tracking in SHANDY UI)
GCP_BILLING_ACCOUNT_ID=015426-0B5674-83F27C

# Application-level budget limits
MAX_PROJECT_SPEND_TOTAL_USD=5000  # Match GCP budget
MAX_PROJECT_SPEND_24H_USD=500     # Prevent daily spikes
```

**Important:**
- Use absolute path for `GOOGLE_APPLICATION_CREDENTIALS`
- Verify path exists and has correct permissions
- Keep existing `PORT`, `STORAGE_SECRET` values

### 2.3: Verify Prerequisites on gassh

```bash
# On gassh:

# 1. Check Docker is running
docker --version
docker-compose --version

# 2. Check network connectivity to Vertex AI
curl -I https://us-east5-aiplatform.googleapis.com/

# 3. Verify service account key file
ls -lh .credentials/vertexai-project-covid-19-277821-b9a24f9376ca.json
# Should show: -rw------- (600 permissions)

# 4. Test gcloud with service account (optional)
export GOOGLE_APPLICATION_CREDENTIALS=$(pwd)/.credentials/vertexai-project-covid-19-277821-b9a24f9376ca.json
gcloud auth application-default print-access-token
# Should print an access token
```

## Phase 3: Deploy to gassh

### 3.1: Pull Latest Code

```bash
# On gassh:
cd /path/to/shandy

# Backup current version
git rev-parse HEAD > .last-deployment-$(date +%Y%m%d-%H%M%S).txt

# Pull latest from main
git fetch origin
git pull origin main

# Verify we got the vertex-ai-budget-enforcer
ls -la vertex-ai-budget-enforcer/
# Should show: main.py, requirements.txt, Dockerfile

# Check commit
git log -1 --oneline
# Should show: "Add Vertex AI budget enforcer..."
```

### 3.2: Rebuild Docker Images

```bash
# On gassh:
cd /path/to/shandy

# Stop current containers
docker-compose down

# Rebuild with new code
docker-compose build --no-cache

# Verify images built
docker images | grep shandy
```

### 3.3: Start Updated Containers

```bash
# On gassh:
docker-compose up -d

# Check containers started
docker-compose ps

# Follow logs
docker-compose logs -f --tail=50
```

**Expected log output:**
- "Serving Flask app 'main'" (web app started)
- No authentication errors
- No "CBORG" mentions (switched to Vertex)

### 3.4: Health Check

```bash
# On gassh:

# 1. Check web interface is up
curl -I http://localhost:8080
# Should return: HTTP/1.1 200 OK

# 2. Check container health
docker inspect shandy-shandy-1 --format='{{.State.Health.Status}}'
# Should return: healthy

# 3. Test Vertex AI connectivity from container
docker-compose exec shandy python -c "
from anthropic import AnthropicVertex
client = AnthropicVertex(
    project_id='test-project-covid-19-277821',
    region='us-east5'
)
print('✓ Vertex AI client initialized')
"
# Should print: ✓ Vertex AI client initialized
```

## Phase 4: Smoke Testing

### 4.1: Test Discovery Job

**Via Web UI:**
1. Navigate to https://shandy.alzassistant.org
2. Upload a small test dataset (e.g., 10-row CSV)
3. Create discovery job with:
   - Max iterations: 5
   - Skills enabled
4. Start job
5. Monitor progress in UI

**Expected behavior:**
- Job starts successfully
- Iterations execute using Vertex AI (Claude Sonnet 4.5)
- Plots generated
- Knowledge graph updated
- Job completes or reaches max iterations

**Logs to check:**
```bash
# On gassh:
docker-compose logs -f shandy

# Look for:
# - "Using Vertex AI provider"
# - "Claude Sonnet 4.5" model references
# - No CBORG references
# - Successful API calls
```

### 4.2: Test Cost Tracking

After job completes:

```bash
# On gassh - check if BigQuery billing export is enabled
docker-compose exec shandy python -c "
from shandy.cost_tracker import get_current_spend
spend = get_current_spend()
print(f'Current spend: {spend}')
"
```

**If BigQuery export is NOT enabled:**
- Cost tracking will return `0.00`
- This is OK - budget enforcer works independently
- See `docs/VERTEX_SETUP.md` Section 3 to enable (optional)

### 4.3: Verify Budget Enforcer

The budget enforcer is already deployed and monitoring:

```bash
# From local machine:

# Check Cloud Run service is running
gcloud run services describe budget-enforcer \
    --region=us-east5 \
    --project=test-project-covid-19-277821

# Check recent logs
gcloud logging read \
    "resource.type=cloud_run_revision AND resource.labels.service_name=budget-enforcer" \
    --limit=10 \
    --project=test-project-covid-19-277821
```

**What to verify:**
- Service is deployed (revision: budget-enforcer-00003-cp5)
- No error logs
- Budget alert subscription is active

## Phase 5: Monitoring and Validation

### 5.1: Monitor First 24 Hours

**Check these metrics:**

1. **Costs** (GCP Console):
   - Go to: https://console.cloud.google.com/billing
   - Check Vertex AI spending
   - Compare to budget ($5,000/month)

2. **Jobs** (SHANDY UI):
   - Track job success rate
   - Monitor iteration counts
   - Check for API errors

3. **Performance**:
   ```bash
   # On gassh:
   docker stats shandy-shandy-1
   # Monitor CPU, memory usage
   ```

4. **Logs**:
   ```bash
   # On gassh:
   docker-compose logs --tail=100 shandy | grep -i error
   # Should be minimal/no errors
   ```

### 5.2: Alert Configuration

Set up email alerts for budget thresholds:

1. Go to [GCP Budgets](https://console.cloud.google.com/billing/budgets)
2. Find: "SHANDY - test-project-covid-19-277821"
3. Edit → Actions → Add email recipients
4. Add your email for alerts at: 50%, 80%, 90%, 100%

This supplements the automatic key disabling at 100%.

### 5.3: Success Criteria

✅ **Deployment successful if:**
- Web UI accessible at https://shandy.alzassistant.org
- Discovery jobs complete successfully
- Using Vertex AI (not CBORG) - check logs
- No authentication errors
- Costs tracking (or documented as not enabled)
- Budget enforcer monitoring (check Cloud Run logs)

❌ **Rollback if:**
- Jobs fail with auth errors → Check service account key path
- API rate limits hit immediately → Check quotas
- Costs spike unexpectedly → Investigate runaway job
- Web UI not accessible → Check Docker containers

## Phase 6: Rollback Procedure (if needed)

If deployment fails and you need to revert:

```bash
# On gassh:
cd /path/to/shandy

# 1. Stop new version
docker-compose down

# 2. Restore old .env
cp .env.backup-cborg-YYYYMMDD .env

# 3. Checkout previous commit
git log --oneline -5  # Find last working commit
git checkout <commit-hash>

# 4. Rebuild old version
docker-compose build
docker-compose up -d

# 5. Verify CBORG is working
docker-compose logs -f
```

**Recovery time:** 5-10 minutes

## Phase 7: Documentation Updates

After successful deployment:

### 7.1: Update Deployment Notes

Create/update on gassh:
```bash
# On gassh:
echo "$(date): Deployed Vertex AI version (commit: $(git rev-parse HEAD))" >> DEPLOYMENT_HISTORY.txt
echo "Using service account: claude-code-sa-2025-11-10@test-project-covid-19-277821.iam.gserviceaccount.com" >> DEPLOYMENT_HISTORY.txt
echo "Budget enforcer: ACTIVE ($5,000/month limit)" >> DEPLOYMENT_HISTORY.txt
```

### 7.2: Share Access

Document for team members:
- URL: https://shandy.alzassistant.org
- Auth: OAuth (Google/GitHub) or mock auth in dev mode
- Budget: $5,000/month shared between test + production
- Recovery contact: (your email for budget alerts)

## Risk Assessment

### Shared Budget Impact

**⚠️ CRITICAL CONSIDERATION:**

Because test and production share the same $5,000/month budget:

**Scenario 1: Testing spike**
- You run intensive testing locally (Vertex AI)
- Test environment hits $2,000 in one week
- Production (gassh) has only $3,000 left for the month
- If production uses $3,000+, budget enforcer disables keys
- **Both environments go down**

**Scenario 2: Production spike**
- Heavy usage on shandy.alzassistant.org
- Production hits $5,000 budget
- Budget enforcer triggers
- Production AND your local testing both stop working

**Mitigation strategies:**
1. **Monitor spending daily** - Check GCP Console billing
2. **Set email alerts** at 50%, 80% thresholds (Phase 5.2)
3. **Limit test iterations** - Use small jobs during testing
4. **Reserve budget** - Mentally allocate $4,000 prod, $1,000 test
5. **Plan for next month** - If approaching limit, wait for new billing period

**If budget exceeded:**
- Follow recovery procedure in `notes/budget-enforcer-test-plan.md` Phase 7
- Re-enable keys: `gcloud iam service-accounts keys enable <KEY_ID> ...`
- Consider creating separate service account for production (Phase 8 below)

## Phase 8: Future Improvements (Optional)

### Separate Service Account for Production

If shared budget becomes problematic:

**Benefits:**
- Independent $5,000 budgets for test and prod
- Test environment failures don't affect production
- Clearer cost attribution

**Implementation:**
1. Create new service account: `shandy-prod@test-project-covid-19-277821.iam.gserviceaccount.com`
2. Create new JSON key
3. Deploy separate budget enforcer for prod SA
4. Set up separate $5,000 budget for prod
5. Update gassh `.env` to use new SA
6. Test and validate

**Estimated time:** 1-2 hours

## Timeline Estimate

**Phase 1 (Commit & Push):** 15 minutes
**Phase 2 (Prepare gassh):** 30 minutes
**Phase 3 (Deploy):** 30 minutes
**Phase 4 (Smoke test):** 30 minutes
**Phase 5 (Monitor):** Ongoing (24h)

**Total deployment time:** ~2 hours
**Rollback time (if needed):** 10 minutes

## Checklist

### Pre-Deployment
- [ ] All changes committed to main branch
- [ ] Changes pushed to GitHub
- [ ] No secrets committed (verify .gitignore)
- [ ] Service account key available locally
- [ ] SSH access to gassh verified

### During Deployment
- [ ] Service account key copied to gassh
- [ ] .env updated on gassh
- [ ] Prerequisites verified (Docker, network, keys)
- [ ] Latest code pulled from main
- [ ] Docker images rebuilt
- [ ] Containers started successfully
- [ ] Health checks passed

### Post-Deployment
- [ ] Smoke test job completed successfully
- [ ] Using Vertex AI (verified in logs)
- [ ] Budget enforcer active (checked Cloud Run)
- [ ] Email alerts configured
- [ ] Deployment documented
- [ ] Monitoring started (24h check)

### Validation (24h later)
- [ ] No unexpected errors in logs
- [ ] Jobs completing successfully
- [ ] Costs within budget
- [ ] Performance acceptable
- [ ] Team notified of successful deployment

## Support and Escalation

**If issues arise:**

1. **Check logs:** `docker-compose logs -f shandy`
2. **Check this plan:** Review relevant phase
3. **Check test plan:** `notes/budget-enforcer-test-plan.md`
4. **Rollback if critical:** Follow Phase 6

**Budget enforcement triggered:**
- Follow `notes/budget-enforcer-test-plan.md` Phase 7 (Recovery)
- Re-enable keys manually
- Investigate cause before resuming

**Questions/Issues:**
- Review: `docs/VERTEX_SETUP.md`
- Review: `docs/VERTEX_BUDGET_SAFETY.md`
- Open GitHub issue for bugs

---

**Plan Status:** Draft (ready for review)
**Last Updated:** 2025-11-23
**Author:** Claude Code
