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

# Web app settings
APP_PASSWORD_HASH=your-bcrypt-hash
DISABLE_AUTH=false
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

## Cost Optimization

- **Use Haiku for simple tasks**: Set `ANTHROPIC_SMALL_FAST_MODEL` appropriately
- **Set budget limits**: Use `MAX_PROJECT_SPEND_*` variables to prevent overruns
- **Monitor in GCP Console**: [Billing Reports](https://console.cloud.google.com/billing) shows real-time trends
- **Clean up old jobs**: Run `python -m shandy.job_manager cleanup --days 7`

## Additional Resources

- [Vertex AI Documentation](https://cloud.google.com/vertex-ai/docs)
- [Anthropic on Vertex AI](https://cloud.google.com/vertex-ai/generative-ai/docs/partner-models/use-claude)
- [BigQuery Billing Export](https://cloud.google.com/billing/docs/how-to/export-data-bigquery)
- [GCP IAM Permissions](https://cloud.google.com/iam/docs/understanding-roles)

## Security Best Practices

1. **Least Privilege**: Service account has only required roles
2. **Key Rotation**: Rotate service account keys regularly
3. **Key Storage**: Never commit `.json` keys to git (already in `.gitignore`)
4. **Budget Alerts**: Set up GCP budget alerts in addition to SHANDY limits
5. **Audit Logs**: Enable Cloud Audit Logs for Vertex AI API calls
