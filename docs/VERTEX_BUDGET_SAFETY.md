# GCP Budget Safety Net for Vertex AI

## Overview

This document describes how to implement an automatic safety mechanism to prevent runaway costs when using SHANDY with Google Vertex AI. The safety net automatically disables the service account when spending exceeds budget limits.

**Status**: Not yet implemented. This is a planned enhancement for production deployments.

**Why this is needed**: While SHANDY has application-level budget controls (`MAX_PROJECT_SPEND_TOTAL_USD`, `MAX_PROJECT_SPEND_24H_USD`), these rely on billing data being available via BigQuery export. If billing export is not enabled or has significant lag, costs could exceed limits before detection. This GCP-side safety net provides a hard stop at the infrastructure level.

## Architecture

The safety net uses Google Cloud's native budget alerting integrated with automatic service account key disabling:

```
┌─────────────────────┐
│ GCP Billing Budget  │  Monitor spend, trigger at 100%
└──────────┬──────────┘
           │ Budget alert
           ▼
┌─────────────────────┐
│   Pub/Sub Topic     │  Receive budget notifications
└──────────┬──────────┘
           │ Message
           ▼
┌─────────────────────┐
│  Cloud Run Service  │  Execute Python code
└──────────┬──────────┘
           │ API call
           ▼
┌─────────────────────┐
│  IAM Service API    │  Disable service account key
└─────────────────────┘
```

**When budget threshold (100%) is reached:**
1. GCP Billing sends alert to Pub/Sub topic
2. Cloud Run service receives Pub/Sub message
3. Service disables the service account JSON key via IAM API
4. SHANDY can no longer make Vertex AI API calls
5. Admin is notified and can investigate

**Recovery:**
- Keys can be re-enabled via `gcloud` CLI or Cloud Console
- No data loss - SHANDY jobs fail gracefully
- Service can resume once budget is addressed

## Prerequisites

- GCP project with billing enabled
- Service account created for SHANDY (from VERTEX_SETUP.md)
- Billing Account Admin role (to create budget alerts)
- Cloud Run API enabled
- Pub/Sub API enabled

## Implementation Steps

### Step 1: Enable Required APIs

```bash
export PROJECT_ID=your-project-id
gcloud config set project $PROJECT_ID

# Enable Cloud Run (for the budget enforcement function)
gcloud services enable run.googleapis.com

# Enable Pub/Sub (for budget notifications)
gcloud services enable pubsub.googleapis.com

# Enable IAM API (to disable keys programmatically)
gcloud services enable iam.googleapis.com
```

### Step 2: Create Pub/Sub Topic for Budget Alerts

```bash
# Create topic
gcloud pubsub topics create budget-alerts \
    --project=$PROJECT_ID

# Verify creation
gcloud pubsub topics list --project=$PROJECT_ID
```

### Step 3: Create Cloud Run Service to Disable Keys

Create a Python service that disables the service account key when triggered:

**`budget-enforcer/main.py`:**
```python
"""
Cloud Run service to disable service account keys when budget is exceeded.
Triggered by Pub/Sub messages from GCP Billing Budget alerts.
"""
import base64
import json
import os
from google.cloud import iam_admin_v1
from flask import Flask, request

app = Flask(__name__)

# Service account email to disable
SERVICE_ACCOUNT_EMAIL = os.environ.get("SERVICE_ACCOUNT_EMAIL")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID")

@app.route("/", methods=["POST"])
def handle_budget_alert():
    """Handle budget alert from Pub/Sub."""
    envelope = request.get_json()
    if not envelope:
        return "No Pub/Sub message received", 400

    # Decode Pub/Sub message
    if "message" not in envelope:
        return "Invalid Pub/Sub message format", 400

    pubsub_message = envelope["message"]

    # Decode data
    if "data" in pubsub_message:
        data = base64.b64decode(pubsub_message["data"]).decode("utf-8")
        budget_notification = json.loads(data)

        # Check if budget exceeded
        cost_amount = budget_notification.get("costAmount", 0)
        budget_amount = budget_notification.get("budgetAmount", 0)

        if cost_amount >= budget_amount:
            # Budget exceeded - disable service account key
            disable_service_account_keys()
            return f"Budget exceeded (${cost_amount} >= ${budget_amount}). Service account keys disabled.", 200

    return "Budget alert received but threshold not met", 200

def disable_service_account_keys():
    """Disable all keys for the configured service account."""
    client = iam_admin_v1.IAMClient()

    # List keys for service account
    request = iam_admin_v1.ListServiceAccountKeysRequest(
        name=f"projects/{PROJECT_ID}/serviceAccounts/{SERVICE_ACCOUNT_EMAIL}"
    )

    keys = client.list_service_account_keys(request=request)

    # Disable each key
    for key in keys.keys:
        # Skip Google-managed keys (only disable user-managed keys)
        if key.key_type == iam_admin_v1.ServiceAccountKeyType.USER_MANAGED:
            disable_request = iam_admin_v1.DisableServiceAccountKeyRequest(
                name=key.name
            )
            client.disable_service_account_key(request=disable_request)
            print(f"Disabled key: {key.name}")

if __name__ == "__main__":
    # Cloud Run sets PORT environment variable
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
```

**`budget-enforcer/requirements.txt`:**
```
flask>=2.3.0
google-cloud-iam>=2.12.0
```

**`budget-enforcer/Dockerfile`:**
```dockerfile
FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

CMD exec python main.py
```

### Step 4: Deploy Cloud Run Service

```bash
# Build and deploy
cd budget-enforcer

gcloud run deploy budget-enforcer \
    --source . \
    --platform managed \
    --region us-central1 \
    --allow-unauthenticated \
    --set-env-vars SERVICE_ACCOUNT_EMAIL=shandy-vertex@${PROJECT_ID}.iam.gserviceaccount.com,GCP_PROJECT_ID=${PROJECT_ID}

# Note the service URL from deployment output
```

**Security Note**: The service is deployed with `--allow-unauthenticated` because Pub/Sub push subscriptions don't support authentication tokens by default. In production, you should:
1. Use Pub/Sub push authentication with service accounts
2. Or use Pub/Sub pull subscriptions with authenticated polling

### Step 5: Grant IAM Permissions

The Cloud Run service needs permission to disable service account keys:

```bash
# Get the Cloud Run service's service account
# (by default: PROJECT_NUMBER-compute@developer.gserviceaccount.com)
export PROJECT_NUMBER=$(gcloud projects describe $PROJECT_ID --format="value(projectNumber)")
export CLOUD_RUN_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"

# Grant Service Account Key Admin role
gcloud projects add-iam-policy-binding $PROJECT_ID \
    --member="serviceAccount:${CLOUD_RUN_SA}" \
    --role="roles/iam.serviceAccountKeyAdmin"
```

### Step 6: Create Pub/Sub Push Subscription

Link the Pub/Sub topic to the Cloud Run service:

```bash
# Get Cloud Run service URL
export SERVICE_URL=$(gcloud run services describe budget-enforcer \
    --region us-central1 \
    --format="value(status.url)")

# Create push subscription
gcloud pubsub subscriptions create budget-alerts-sub \
    --topic budget-alerts \
    --push-endpoint=$SERVICE_URL \
    --project=$PROJECT_ID
```

### Step 7: Create Billing Budget with Alert

**Via Cloud Console** (recommended):

1. Go to [Cloud Console > Billing > Budgets & alerts](https://console.cloud.google.com/billing/budgets)
2. Click **Create Budget**
3. **Scope**:
   - Projects: Select your SHANDY project
   - Services: Select "Vertex AI" only
4. **Amount**:
   - Budget type: Specified amount
   - Target amount: e.g., $100
   - Include credits: No
5. **Actions**:
   - Threshold rules: 100%
   - Manage notifications → **Connect a Pub/Sub topic**
   - Select: `budget-alerts`
6. Click **Finish**

**Via gcloud** (alternative):

```bash
# Note: Requires billing account ID
export BILLING_ACCOUNT_ID=XXXXXX-YYYYYY-ZZZZZZ

# Create budget (example: $100 USD)
gcloud billing budgets create \
    --billing-account=$BILLING_ACCOUNT_ID \
    --display-name="SHANDY Vertex AI Budget" \
    --budget-amount=100USD \
    --threshold-rule=percent=100 \
    --filter-projects=projects/$PROJECT_ID \
    --filter-services=services/aiplatform.googleapis.com \
    --pubsub-topic=projects/$PROJECT_ID/topics/budget-alerts
```

## Testing

### Test with Low Budget

Set a very low budget (e.g., $1) to test the mechanism without spending much:

```bash
# Create test budget
gcloud billing budgets create \
    --billing-account=$BILLING_ACCOUNT_ID \
    --display-name="SHANDY Test Budget" \
    --budget-amount=1USD \
    --threshold-rule=percent=100 \
    --filter-projects=projects/$PROJECT_ID \
    --filter-services=services/aiplatform.googleapis.com \
    --pubsub-topic=projects/$PROJECT_ID/topics/budget-alerts
```

Run a SHANDY job and monitor:
1. Check Cloud Run logs for incoming budget alerts
2. Verify service account key is disabled when $1 threshold crossed
3. Confirm SHANDY job fails with authentication error

### Manual Testing

Trigger the Cloud Run service manually:

```bash
# Simulate a budget alert
curl -X POST $SERVICE_URL \
  -H "Content-Type: application/json" \
  -d '{
    "message": {
      "data": "eyJjb3N0QW1vdW50IjoxMDAsImJ1ZGdldEFtb3VudCI6MTAwfQ=="
    }
  }'

# The data field is base64-encoded JSON:
# {"costAmount":100,"budgetAmount":100}
```

Check if keys are disabled:

```bash
gcloud iam service-accounts keys list \
    --iam-account=shandy-vertex@${PROJECT_ID}.iam.gserviceaccount.com
```

## Recovery Procedure

When the safety net triggers and disables keys:

### 1. Investigate the Cause

Check spending in [GCP Billing Reports](https://console.cloud.google.com/billing):
- What caused the spike?
- Was it a runaway job?
- Is the budget too low?

### 2. Re-enable Service Account Key

**Via gcloud:**

```bash
# List keys (will show disabled keys)
gcloud iam service-accounts keys list \
    --iam-account=shandy-vertex@${PROJECT_ID}.iam.gserviceaccount.com

# Note the KEY_ID from the output

# Re-enable the key
gcloud iam service-accounts keys enable KEY_ID \
    --iam-account=shandy-vertex@${PROJECT_ID}.iam.gserviceaccount.com
```

**Via Cloud Console:**

1. Go to [IAM & Admin > Service Accounts](https://console.cloud.google.com/iam-admin/serviceaccounts)
2. Click on `shandy-vertex@...`
3. Go to **Keys** tab
4. Find the disabled key
5. Click **Enable**

### 3. Adjust Budget if Needed

If the budget was too conservative:

```bash
# Update budget
gcloud billing budgets update BUDGET_ID \
    --billing-account=$BILLING_ACCOUNT_ID \
    --budget-amount=500USD
```

### 4. Resume SHANDY Operations

Once the key is re-enabled:
- SHANDY can immediately resume making Vertex AI calls
- Queued jobs will automatically retry
- No application restart needed

## Monitoring and Alerts

### Cloud Run Logs

Monitor Cloud Run service execution:

```bash
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=budget-enforcer" \
    --limit 50 \
    --format json
```

### Pub/Sub Metrics

Check if budget alerts are being delivered:

```bash
gcloud monitoring time-series list \
    --filter='resource.type="pubsub_subscription" AND resource.labels.subscription_id="budget-alerts-sub"'
```

### Email Notifications

In addition to the automatic key disabling, configure email alerts:

1. Go to [Cloud Console > Budgets & alerts](https://console.cloud.google.com/billing/budgets)
2. Edit your budget
3. Under **Actions**, add email recipients
4. This sends notifications at threshold percentages (50%, 90%, 100%)

## Important Considerations

### Service Disruption

- **Impact**: When keys are disabled, ALL SHANDY jobs fail immediately
- **Data loss**: Jobs in progress will fail, potentially losing partial results
- **Recovery time**: Minutes (time to investigate + re-enable key)

**Recommendation**: Set budget thresholds with sufficient headroom to allow manual intervention before automatic cutoff.

### Budget Lag

GCP billing data has a lag (typically 1-6 hours). The safety net triggers based on billing data, not real-time API usage.

**Implication**: If you make $200 in API calls in 5 minutes, the budget alert may not trigger for hours. By then, spending could be much higher.

**Mitigation**:
1. Use application-level controls (SHANDY's `MAX_PROJECT_SPEND_24H_USD`) for real-time protection
2. Set GCP budget as a hard backstop, not primary protection

### Multiple Environments

If running multiple SHANDY instances (dev, staging, prod):

**Option 1**: Separate projects (recommended)
- Each environment in its own GCP project
- Independent budgets and safety nets
- Clear cost attribution

**Option 2**: Shared project with labels
- Use resource labels to separate environments
- More complex budget filtering
- Risk of dev spending affecting prod budget

### Key Rotation Consideration

If you rotate service account keys regularly (security best practice):
- Update `GOOGLE_APPLICATION_CREDENTIALS` in `.env`
- Old keys will be disabled by rotation anyway
- Safety net works with any user-managed key for the service account

## Cost Optimization Tips

The safety net itself incurs minimal costs:

- **Cloud Run**: Free tier covers ~2 million requests/month
- **Pub/Sub**: Free tier covers 10 GB/month
- **Budget alerts**: Free

Expected monthly cost for safety net: **$0** (within free tier)

## Additional Resources

- [GCP Budgets and Alerts](https://cloud.google.com/billing/docs/how-to/budgets)
- [Pub/Sub Push Subscriptions](https://cloud.google.com/pubsub/docs/push)
- [IAM Service Account Keys](https://cloud.google.com/iam/docs/keys-create-delete)
- [Cloud Run Documentation](https://cloud.google.com/run/docs)

## Future Enhancements

Potential improvements to this safety net:

1. **Graceful degradation**: Instead of hard cutoff, throttle to cheaper models (Haiku instead of Sonnet)
2. **Smart alerting**: Send Slack/PagerDuty alerts before disabling keys
3. **Automatic re-enabling**: Re-enable keys at start of new billing period
4. **Per-job budgets**: Track costs per job and kill expensive jobs before they exhaust budget
5. **Predictive alerts**: Use ML to predict when budget will be exceeded based on current burn rate
