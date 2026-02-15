# Google Vertex AI Setup for SHANDY

This guide explains how to configure SHANDY to use Google Cloud Vertex AI instead of CBORG for model access.

## Overview

Vertex AI provides access to Claude models through Google Cloud. SHANDY integrates with Vertex AI for model calls and optionally uses BigQuery billing export for cost tracking.

### What is BigQuery Billing Export?

**Purpose**: BigQuery billing export is used for **cost tracking** in SHANDY. Unlike CBORG (which provides real-time cost APIs), Vertex AI doesn't have a direct cost API. Instead, GCP exports billing data to BigQuery tables, which SHANDY queries to show your spending.

**Is it required?** **NO** - BigQuery billing export is **completely optional**:
- ✅ **Without it**: SHANDY works perfectly fine. Jobs will run normally using Vertex AI. You just won't see cost information in the web UI.
- ✅ **With it**: You get cost tracking in the web UI (total spend, last 24h spend, budget warnings)

**Data lag**: GCP billing data has a 1-6 hour lag. The web UI displays an estimated data freshness timestamp when billing export is configured.

## Prerequisites

- Google Cloud account
- GCP project with billing enabled
- `gcloud` CLI installed and authenticated
- Owner or Editor permissions on the project

## Step 1: Enable Required APIs

**IMPORTANT**: Use your personal account (not a service account) to enable APIs and manage billing:

```bash
# Check which account is active
gcloud auth list

# If using a service account, switch to your personal account
gcloud config set account YOUR_EMAIL@example.com

# Set your project ID
export PROJECT_ID=your-project-id
gcloud config set project $PROJECT_ID

# Enable Vertex AI API (REQUIRED - for model access)
gcloud services enable aiplatform.googleapis.com

# Enable Cloud Billing API (REQUIRED - to list billing accounts)
# This allows SHANDY to query your billing account ID
gcloud services enable cloudbilling.googleapis.com

# Enable BigQuery API (OPTIONAL - only if you want cost tracking)
gcloud services enable bigquery.googleapis.com
```

**Why switch accounts?** Service accounts typically don't have permissions to enable APIs or list billing accounts. Use your personal account for setup, then the service account will be used at runtime.

## Step 2: Create Service Account

Create a service account with permissions to call Vertex AI and read billing data:

```bash
# Create service account
gcloud iam service-accounts create shandy-vertex \
    --display-name="SHANDY Vertex AI Service Account"

# Grant Vertex AI User role
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:shandy-vertex@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/aiplatform.user"

# Grant BigQuery Data Viewer role (for billing data)
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:shandy-vertex@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/bigquery.dataViewer"

# Grant BigQuery Job User role (to run queries)
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:shandy-vertex@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/bigquery.jobUser"

# Download service account key
gcloud iam service-accounts keys create ~/shandy-vertex-key.json \
    --iam-account=shandy-vertex@${PROJECT_ID}.iam.gserviceaccount.com
```

**Security Note**: Store the service account key securely. Do not commit it to git.

## Step 3: Enable BigQuery Billing Export (OPTIONAL)

**This step is optional.** If you skip this, SHANDY will work fine but won't display cost information in the web UI.

Cost tracking in SHANDY uses BigQuery billing export:

### 3.1 Enable Billing Export in Console

1. Go to [Google Cloud Console > Billing](https://console.cloud.google.com/billing)
2. Select your billing account
3. Click **Billing export** in the left menu
4. Click **Edit settings** under **BigQuery export**
5. Enable **Detailed usage cost**
6. Select or create a BigQuery dataset (e.g., `billing_export`)
7. Click **Save**

### 3.2 Find Your Billing Account ID

```bash
# Make sure you're using your personal account (not service account)
gcloud auth list
# If needed, switch to personal account:
# gcloud config set account YOUR_EMAIL@example.com

# List billing accounts
gcloud billing accounts list

# Note the ACCOUNT_ID (format: XXXXXX-YYYYYY-ZZZZZZ)
```

**Troubleshooting**: If you get "API not enabled" error:
```bash
# Enable the Cloud Billing API (must be done with personal account)
gcloud services enable cloudbilling.googleapis.com --project=YOUR_PROJECT_ID

# Then try again:
gcloud billing accounts list
```

Add the billing account ID to your `.env`:
```bash
GCP_BILLING_ACCOUNT_ID=XXXXXX-YYYYYY-ZZZZZZ
```

### 3.3 Verify Billing Export Table

After enabling export, billing data will begin populating in BigQuery. The table name will be:

```
your-project-id.billing_export.gcp_billing_export_v1_XXXXXX_YYYYYY_ZZZZZZ
```

Where `XXXXXX_YYYYYY_ZZZZZZ` is your billing account ID with hyphens replaced by underscores.

**Note**: Initial billing data may take 1-24 hours to appear after enabling export.

## Step 4: Check Claude Model Availability

Verify Claude models are available in your region:

```bash
# Check available models in us-east5 (recommended for Claude)
gcloud ai models list \
    --region=us-east5 \
    --filter="displayName:claude"
```

Recommended regions for Claude models:
- **us-east5**: Primary region for Sonnet and Haiku
- **us-central1**: Alternative region
- **europe-west1**: European alternative

## Step 5: Configure SHANDY Environment

Create or update your `.env` file:

```bash
#############################################
# Provider: Google Vertex AI
#############################################

# Provider selection
CLAUDE_PROVIDER=vertex

# Vertex AI configuration
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project-id
GOOGLE_APPLICATION_CREDENTIALS=/path/to/shandy-vertex-key.json
CLOUD_ML_REGION=us-east5
VERTEX_REGION_CLAUDE_4_5_SONNET=us-east5
VERTEX_REGION_CLAUDE_4_5_HAIKU=us-east5

# Model selection (optional, these are defaults)
ANTHROPIC_MODEL=claude-sonnet-4-5@20250929
ANTHROPIC_SMALL_FAST_MODEL=claude-haiku-4-5@20251001

# BigQuery billing export
GCP_BILLING_ACCOUNT_ID=XXXXXX-YYYYYY-ZZZZZZ

# Budget controls (optional)
MAX_PROJECT_SPEND_TOTAL_USD=1000
MAX_PROJECT_SPEND_24H_USD=50

# Web app settings - use mock auth for development
ENABLE_MOCK_AUTH=true
```

### Environment Variable Reference

| Variable | Required | Description |
|----------|----------|-------------|
| `CLAUDE_PROVIDER` | Yes | Set to `vertex` |
| `ANTHROPIC_VERTEX_PROJECT_ID` | Yes | Your GCP project ID |
| `GOOGLE_APPLICATION_CREDENTIALS` | Yes | Path to service account JSON key |
| `CLOUD_ML_REGION` | Yes | Region for Vertex AI (e.g., `us-east5`) |
| `VERTEX_REGION_CLAUDE_4_5_SONNET` | Recommended | Region for Sonnet model |
| `VERTEX_REGION_CLAUDE_4_5_HAIKU` | Recommended | Region for Haiku model |
| `GCP_BILLING_ACCOUNT_ID` | Required for cost tracking | Billing account ID (format: XXXXXX-YYYYYY-ZZZZZZ) |
| `MAX_PROJECT_SPEND_TOTAL_USD` | Optional | Budget limit for total spend |
| `MAX_PROJECT_SPEND_24H_USD` | Optional | Budget limit for 24-hour spend |

## Step 6: Test Configuration

### 6.1 Test Service Account Permissions

```bash
# Activate service account
gcloud auth activate-service-account \
    --key-file=/path/to/shandy-vertex-key.json

# Test Vertex AI access
gcloud ai models list --region=us-east5 --limit=5

# Test BigQuery access
bq ls billing_export
```

### 6.2 Test SHANDY Provider

Start SHANDY and check the logs:

```bash
# With Docker
docker-compose up -d
docker logs shandy-shandy-1

# You should see:
# INFO - Vertex AI provider environment configured
# INFO - Web app initialized
```

Visit http://localhost:8080/new and check the budget information card. It should display:
- Provider: Vertex AI
- Total Spend: $X.XX
- Last 24h: $X.XX
- Data lag note: "Data current as of ~..."

### 6.3 Test Job Creation

1. Upload a small data file
2. Enter a research question
3. Set max iterations to 2 (for quick test)
4. Click "Start Discovery"

Monitor the logs for Vertex AI API calls:

```bash
docker logs -f shandy-shandy-1
```

## Troubleshooting

### "Permission denied" errors

**Cause**: Service account lacks required permissions.

**Solution**:
```bash
# Re-add IAM roles
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:shandy-vertex@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/aiplatform.user"
```

### "Billing data unavailable" or cost tracking warnings

**Symptom**: Logs show "Failed to fetch cost info from Vertex AI" or web UI doesn't display cost information.

**Is this a problem?** **NO** - This is completely normal if you haven't set up BigQuery billing export (Step 3). SHANDY will work fine; you just won't see cost tracking in the web UI.

**If you want cost tracking:**
1. Verify billing export is enabled in Cloud Console (see Step 3.1)
2. Wait 1-24 hours for initial data to populate after enabling
3. Ensure `GCP_BILLING_ACCOUNT_ID` is set in your `.env`
4. Check table exists:
   ```bash
   bq ls --project_id=$PROJECT_ID billing_export
   ```

### "Model not found" or "Region not supported"

**Cause**: Claude model not available in specified region.

**Solution**:
1. Try different region (us-east5, us-central1, europe-west1)
2. Check available models:
   ```bash
   gcloud ai models list --region=us-east5 --filter="displayName:claude"
   ```

### Cost data shows $0.00

**Possible causes**:
1. **Billing export delay**: Wait 1-6 hours after API calls
2. **Wrong billing account**: Verify `GCP_BILLING_ACCOUNT_ID` matches your project's billing account
3. **Table name mismatch**: Billing account ID should have underscores, not hyphens

**Debugging**:
```bash
# Check billing table name
bq ls --project_id=$PROJECT_ID billing_export

# Query recent Vertex AI charges
bq query --project_id=$PROJECT_ID \
  "SELECT service.description, SUM(cost) as total_cost
   FROM \`billing_export.gcp_billing_export_v1_*\`
   WHERE service.description = 'Vertex AI'
   GROUP BY service.description"
```

## Budget Protection: Quotas and Safety Nets

SHANDY uses a **two-layer protection system** to prevent runaway costs on Vertex AI:

1. **Layer 1: GCP Quotas** (instant enforcement, hard limits)
2. **Layer 2: Pub/Sub Budget Safety Net** (monthly backstop, auto-disables service account)

### Layer 1: Setting GCP Quotas

GCP quotas provide **instant, hard limits** on API usage. These prevent runaway costs even if application-level budget tracking fails.

#### Current Pricing (as of 2025)

- **Claude Sonnet 4.5**: $3/M input tokens, $15/M output tokens
- **Claude Haiku 4.5**: $0.80/M input tokens, $4/M output tokens

#### Recommended Quota Configuration

Set quotas via the [GCP Quotas Console](https://console.cloud.google.com/iam-admin/quotas):

**Filter for:**
```
Service: Vertex AI API
base_model: anthropic-claude
```

**Quotas to set (both Global and Regional):**

For **Claude Sonnet 4.5** (`anthropic-claude-sonnet-4-5`):
- Global online prediction input tokens per minute: **1,000,000**
- Global online prediction output tokens per minute: **100,000**
- Global online prediction requests per minute: **1,000**
- Regional (us-east5) - set to same values as global
- **Max cost: $270/hour = $6,480/day**

For **Claude Haiku 4.5** (`anthropic-claude-haiku-4-5`):
- Global online prediction input tokens per minute: **3,000,000**
- Global online prediction output tokens per minute: **300,000**
- Global online prediction requests per minute: **3,000**
- Regional (us-east5) - set to same values as global
- **Max cost: $216/hour = $5,184/day**

**Regional request caps** (us-east5):
- Online prediction requests per minute per region: **4,000**
- Long running online prediction requests per minute per region: **4,000**

**Total maximum spend: $11,664/day** (if both models maxed out 24/7)

#### Why Set Both Global and Regional?

- **Global quotas**: Apply across all regions combined, prevent bypassing limits by switching regions
- **Regional quotas**: Apply only to specific region (e.g., us-east5)
- **Both must match**: The more restrictive quota wins, so set them to the same values

#### How to Adjust Quotas

1. Go to: https://console.cloud.google.com/iam-admin/quotas
2. Filter for: `Service: Vertex AI API`, `base_model: anthropic-claude`
3. For each quota:
   - Check the box next to the quota
   - Click "EDIT QUOTAS"
   - Enter new value
   - Add justification: "Budget safety net"
   - Click "SUBMIT REQUEST"
4. Changes take effect immediately (no approval needed for decreases)

### Layer 2: Pub/Sub Budget Safety Net

The budget safety net **automatically disables your Vertex AI service account key** when monthly spending hits a threshold, stopping all API calls.

#### Architecture

```
Monthly spending hits $5k → Budget alert → Pub/Sub topic → Cloud Run service → Disables service account key → API stops
```

**Components:**
1. GCP Billing Budget ($5,000/month for Vertex AI)
2. Pub/Sub topic (`budget-alerts`)
3. Cloud Run service (`budget-enforcer`) - Python app that disables keys
4. Service account key that gets disabled

#### Implementation Steps

**1. Enable Required APIs:**
```bash
gcloud services enable run.googleapis.com pubsub.googleapis.com \
  iam.googleapis.com cloudbuild.googleapis.com \
  artifactregistry.googleapis.com billingbudgets.googleapis.com \
  --project=YOUR_PROJECT_ID
```

**2. Create Pub/Sub Topic:**
```bash
gcloud pubsub topics create budget-alerts --project=YOUR_PROJECT_ID
```

**3. Create Budget Enforcer Service:**

Create `budget-enforcer/main.py`:
```python
"""
Cloud Run service to disable service account keys when budget is exceeded.
"""
import base64
import json
import os
from google.cloud import iam_admin_v1
from flask import Flask, request

app = Flask(__name__)

SERVICE_ACCOUNT_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")

@app.route("/", methods=["POST"])
def handle_budget_alert():
    envelope = request.get_json()
    if not envelope or "message" not in envelope:
        return "Invalid Pub/Sub message", 400

    pubsub_message = envelope["message"]
    if "data" in pubsub_message:
        data = base64.b64decode(pubsub_message["data"]).decode("utf-8")
        budget_notification = json.loads(data)

        cost_amount = budget_notification.get("costAmount", 0)
        budget_amount = budget_notification.get("budgetAmount", 0)

        print(f"Budget notification: cost=${cost_amount}, budget=${budget_amount}")

        if cost_amount >= budget_amount:
            print(f"BUDGET EXCEEDED! Disabling service account keys...")
            disable_service_account_keys()
            return f"Budget exceeded. Service account keys disabled.", 200

    return "Budget alert received but threshold not met", 200

def disable_service_account_keys():
    client = iam_admin_v1.IAMClient()
    request = iam_admin_v1.ListServiceAccountKeysRequest(
        name=f"projects/{PROJECT_ID}/serviceAccounts/{SERVICE_ACCOUNT_EMAIL}"
    )
    keys = client.list_service_account_keys(request=request)

    for key in keys.keys:
        if key.key_type == iam_admin_v1.ServiceAccountKeyType.USER_MANAGED:
            disable_request = iam_admin_v1.DisableServiceAccountKeyRequest(
                name=key.name
            )
            client.disable_service_account_key(request=disable_request)
            print(f"Disabled key: {key.name}")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
```

Create `budget-enforcer/requirements.txt`:
```
flask>=2.3.0
google-cloud-iam>=2.12.0
```

Create `budget-enforcer/Dockerfile`:
```dockerfile
FROM python:3.11-slim
WORKDIR /app

# Install uv for fast Python package management
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:$PATH"

COPY requirements.txt .
RUN uv pip install --system -r requirements.txt
COPY main.py .
CMD exec python main.py
```

**4. Deploy Cloud Run Service:**
```bash
cd budget-enforcer
gcloud run deploy budget-enforcer \
  --source . \
  --platform managed \
  --region us-east5 \
  --allow-unauthenticated \
  --set-env-vars SERVICE_ACCOUNT_EMAIL=shandy-vertex@YOUR_PROJECT_ID.iam.gserviceaccount.com,GCP_PROJECT_ID=YOUR_PROJECT_ID \
  --project=YOUR_PROJECT_ID
```

Note the service URL from the output (e.g., `https://budget-enforcer-123456.us-east5.run.app`)

**5. Grant IAM Permissions:**
```bash
export PROJECT_NUMBER=$(gcloud projects describe YOUR_PROJECT_ID --format="value(projectNumber)")
export CLOUD_RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

gcloud projects add-iam-policy-binding YOUR_PROJECT_ID \
  --member="serviceAccount:${CLOUD_RUN_SA}" \
  --role="roles/iam.serviceAccountKeyAdmin"
```

**6. Create Pub/Sub Push Subscription:**
```bash
export SERVICE_URL=https://budget-enforcer-123456.us-east5.run.app  # From step 4

gcloud pubsub subscriptions create budget-alerts-sub \
  --topic budget-alerts \
  --push-endpoint=$SERVICE_URL \
  --project=YOUR_PROJECT_ID
```

**7. Create Billing Budget (via Console UI):**

Go to: https://console.cloud.google.com/billing/budgets

1. Click "CREATE BUDGET"
2. **Scope**:
   - Projects: Select your project
   - Services: Select "Vertex AI API" only
3. **Amount**:
   - Budget type: Specified amount
   - Target amount: **$5,000** (monthly)
   - Include credits: No
4. **Actions**:
   - Threshold rules: 100%
   - Email notification options:
     - ☐ "Email alerts to billing admins and users" - **Uncheck** to avoid notifying billing account admins
     - ☑ "Email alerts to project owners" - **Check** to notify only project owners
   - **CRITICAL**: Check "Connect a Pub/Sub topic"
   - Select: `budget-alerts`
5. Click "FINISH"

**Note about email notifications:**
- "Billing admins and users" sends to people with billing account roles (may include external Google contacts)
- "Project owners" sends only to people with `roles/owner` on your project
- **The Pub/Sub topic is what actually disables the key** - emails are just FYI notifications
- You can uncheck both email options if you don't want any emails (key will still be disabled automatically)

**Note:** Creating budgets via CLI requires Billing Account Admin permissions, which may not be available.

#### Verifying the Budget

```bash
# List budgets for your billing account
gcloud billing budgets list --billing-account=XXXXXX-YYYYYY-ZZZZZZ

# Look for budget with:
# - displayName: Contains your project name
# - amount: units: '5000'
# - pubsubTopic: projects/YOUR_PROJECT_ID/topics/budget-alerts
```

#### How It Works

1. Your app makes Vertex AI API calls using the service account key
2. When monthly Vertex AI spending reaches **$5,000**:
   - GCP Billing Budget detects threshold crossed
   - Sends notification to Pub/Sub topic `budget-alerts`
   - Pub/Sub pushes message to Cloud Run service
   - Cloud Run service calls IAM API to disable service account key
3. Your app's next API call fails with authentication error
4. No more spending possible until you manually re-enable the key

#### Important Limitations

- **Monthly cumulative, not daily**: Budget tracks total spending for the month, not daily rate
  - Example: Spend $5k in first 3 days → triggers on day 3, not day 1
- **1-6 hour billing lag**: GCP billing data updates with delay
  - If you spend $10k in 10 minutes, alert may not fire for hours
  - **This is why quotas (Layer 1) are the primary protection**
- **Manual recovery required**: Key must be re-enabled manually (see below)

#### Re-enabling Service Account Key

When the safety net triggers, you must manually re-enable the key:

**Via gcloud CLI:**
```bash
# 1. List keys to find the disabled one
gcloud iam service-accounts keys list \
  --iam-account=shandy-vertex@YOUR_PROJECT_ID.iam.gserviceaccount.com

# Output shows KEY_ID and status

# 2. Re-enable the key (replace KEY_ID with actual ID from above)
gcloud iam service-accounts keys enable KEY_ID \
  --iam-account=shandy-vertex@YOUR_PROJECT_ID.iam.gserviceaccount.com
```

**Via Console UI:**
1. Go to: https://console.cloud.google.com/iam-admin/serviceaccounts
2. Click on your service account
3. Go to **Keys** tab
4. Find the disabled key → Click **Enable**

Once re-enabled, your app can immediately resume making API calls.

#### Monitoring

Check Cloud Run logs to see when budget enforcer runs:
```bash
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=budget-enforcer" \
  --limit=50 \
  --project=YOUR_PROJECT_ID
```

### Summary: Two-Layer Protection

| Layer | Type | Limit | Enforcement | Use Case |
|-------|------|-------|-------------|----------|
| **Quotas** | Token/request rate limits | $11,664/day max | Instant | Primary protection against runaway jobs |
| **Pub/Sub Budget** | Monthly spending cap | $5,000/month | 1-6 hour lag | Backstop for sustained overspending |

**Example scenarios:**

- **Single-day spike**: Job burns through 10M tokens in 1 hour
  - ✅ Quotas stop it at 1M tokens/min
  - ❌ Budget won't trigger (only at $180 for the month)

- **Multi-week sustained overspending**: Bug causes $300/day spending for 2 weeks
  - ✅ Quotas allow it (under daily max)
  - ✅ Budget triggers after ~17 days ($5,100 total)

**Best practice**: Rely on quotas for instant protection, use budget as final backstop.

## Cost Optimization

- **Use Haiku for simple tasks**: Set `ANTHROPIC_SMALL_FAST_MODEL` appropriately
- **Set budget limits**: Use `MAX_PROJECT_SPEND_*` variables to prevent overruns
- **Monitor in GCP Console**: [Billing Reports](https://console.cloud.google.com/billing) shows real-time trends
- **Clean up old jobs**: Run `python -m shandy.job_manager cleanup --days 7`
- **Review quotas regularly**: Adjust based on actual usage patterns

## Additional Resources

- [VERTEX_BUDGET_SAFETY.md](VERTEX_BUDGET_SAFETY.md) - Automatic budget safety net for production deployments
- [Vertex AI Documentation](https://cloud.google.com/vertex-ai/docs)
- [Anthropic on Vertex AI](https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-claude)
- [BigQuery Billing Export](https://cloud.google.com/billing/docs/how-to/export-data-bigquery)
- [GCP IAM Permissions](https://cloud.google.com/iam/docs/understanding-roles)

## Security Best Practices

1. **Least Privilege**: Service account has only required roles
2. **Key Rotation**: Rotate service account keys regularly
3. **Key Storage**: Never commit `.json` keys to git (already in `.gitignore`)
4. **Budget Alerts**: Set up GCP budget alerts in addition to SHANDY limits. For production deployments, consider implementing the automatic safety net described in [VERTEX_BUDGET_SAFETY.md](VERTEX_BUDGET_SAFETY.md) to automatically disable service account keys when budget is exceeded
5. **Audit Logs**: Enable Cloud Audit Logs for Vertex AI API calls
