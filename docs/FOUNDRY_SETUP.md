# Azure AI Foundry Setup for SHANDY

**Last Updated**: February 14, 2026

> **Note**: Azure's UI and processes change frequently. If the steps below don't match what you see in the Azure portal, check the [official Microsoft documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/foundry-models/how-to/use-foundry-models-claude) for the latest instructions.

This guide walks you through setting up Azure AI Foundry (Microsoft Foundry) to use Claude models with SHANDY.

## Prerequisites

### Required Azure Subscription

**Important**: Claude models in Azure Foundry require one of these subscription types:
- **Enterprise Agreement (EA)** ✅
- **Microsoft Customer Agreement - Enterprise (MCA-E)** ✅

**Not supported**:
- Pay-As-You-Go subscriptions
- Azure for Students / Free credits (unless part of EA/MCA-E)
- CSP (Cloud Solution Provider) subscriptions
- Sponsored accounts

### Check Your Subscription Type

1. Go to [Azure Portal](https://portal.azure.com)
2. Navigate to **Subscriptions**
3. Select your subscription
4. Look for **"Offer"** - should show "Enterprise Agreement" (MS-AZR-0017P) or similar

### Using CloudBank

If you have access through **CloudBank** (research computing platform):
- CloudBank provides Enterprise Agreement subscriptions ✅
- Your subscription will work with Azure Foundry
- Use your CloudBank Azure login credentials

### Supported Regions

Claude models are **only available** in:
- **East US 2** (recommended)
- **Sweden Central**

## Setup Steps

### Step 1: Access Microsoft Foundry Portal

1. Go to [Microsoft Foundry portal](https://ai.azure.com/)
2. Sign in with your Azure account (CloudBank credentials if applicable)

### Step 2: Deploy Claude Models

**Note**: In the new Microsoft Foundry experience (as of Feb 2026), you don't create a "project" first - you deploy models directly.

1. On the homepage, you'll see **"Latest models"** with Claude models displayed
2. Click on **claude-opus-4-6** (or search in the model catalog)
3. Click **"Use this model"**
4. You'll see a dialog: **"Select your project"**
5. Click **"Create new project"** or similar option
6. Wait for Azure to provision your Foundry environment (2-5 minutes)

### Step 3: Configure Deployment

When the deployment configuration screen appears:

1. **Deployment name**: Use simple names like `claude-opus-4-6`
   - This is what you'll reference in your code
   - Keep it consistent with the model name

2. **Deployment type**: Select **"Global Standard"**
   - This provides pay-per-use pricing with highest rate limits

3. **Resource location**: Choose **East US 2** (or Sweden Central)
   - These are the only supported regions for Claude

4. **Authentication type**: **Key** (recommended for getting started)
   - Or use Microsoft Entra ID for production

5. Click **"Deploy"**

### Step 4: Deploy All Required Models

Repeat Step 2-3 for each model you want to use:

| Model | Deployment Name | Use Case |
|-------|----------------|----------|
| Claude Opus 4.6 | `claude-opus-4-6` | Most capable, complex reasoning |
| Claude Opus 4.5 | `claude-opus-4-5` | Alternative flagship model |
| Claude Sonnet 4.5 | `claude-sonnet-4-5` | Balanced performance/cost |
| Claude Haiku 4.5 | `claude-haiku-4-5` | Fast, cost-effective |

**Recommended minimum**: Deploy at least Opus 4.6, Sonnet 4.5, and Haiku 4.5.

### Step 5: Get Your Credentials

After deploying a model:

1. Click on the deployed model in your dashboard
2. Find the **"Endpoint"** section, which shows:
   - **Target URI**: `https://<resource-name>.services.ai.azure.com/anthropic/v1/messages`
   - **Key**: Your API key (click to reveal/copy)

3. **Note these values**:
   - **Resource name**: The part before `.services.ai.azure.com`
   - **API Key**: The long string (all models share the same key)
   - **Base URL**: `https://<resource-name>.services.ai.azure.com/anthropic`

**Example**:
- Resource name: `cloudbank-shandy-claude-resource`
- Base URL: `https://cloudbank-shandy-claude-resource.services.ai.azure.com/anthropic`

## SHANDY Configuration

### Environment Variables

Add these to your `.env` file:

```bash
#############################################
# AZURE FOUNDRY CONFIGURATION
#############################################

# Provider selection
CLAUDE_PROVIDER=foundry

# REQUIRED: Enable Claude Code Foundry mode
CLAUDE_CODE_USE_FOUNDRY=1

# Azure Foundry resource configuration
# IMPORTANT: Set EITHER resource name OR base URL, NOT both (they're mutually exclusive)
ANTHROPIC_FOUNDRY_RESOURCE=your-resource-name  # Just the name, not full URL
# ANTHROPIC_FOUNDRY_BASE_URL=https://your-resource.services.ai.azure.com/anthropic  # Don't set if using RESOURCE
ANTHROPIC_FOUNDRY_API_KEY=your-api-key-here

# Model deployment names (must match what you created in Azure)
ANTHROPIC_DEFAULT_OPUS_MODEL=claude-opus-4-6
ANTHROPIC_DEFAULT_SONNET_MODEL=claude-sonnet-4-5
ANTHROPIC_DEFAULT_HAIKU_MODEL=claude-haiku-4-5

# For cost tracking via Azure Cost Management API (recommended)
# Find your subscription ID in Azure Portal > Subscriptions
AZURE_SUBSCRIPTION_ID=your-subscription-id
```

### Docker Configuration

The `docker-compose.yml` automatically passes these environment variables to the container. Just ensure your `.env` file is configured correctly.

### Verify Configuration

Start the application and check the provider status on the admin page, or run:

```bash
uv run python -c "from shandy.providers import get_provider; p = get_provider(); print(p.provider_name, p._validate_required_config())"
```

## Rate Limits and Quotas

Default rate limits for Enterprise/MCA-E subscriptions (as of Feb 2026):

| Model | Requests/min | Tokens/min |
|-------|-------------|-----------|
| Claude Opus 4.6 | 2,000 | 2,000,000 |
| Claude Opus 4.5 | 2,000 | 2,000,000 |
| Claude Sonnet 4.5 | 4,000 | 2,000,000 |
| Claude Haiku 4.5 | 4,000 | 4,000,000 |

**To increase limits**: Submit a [quota increase request](https://aka.ms/oai/stuquotarequest)

## Authentication Options

### Option 1: API Key (Recommended for Development)

```bash
ANTHROPIC_FOUNDRY_API_KEY=your-api-key
```

**Pros**: Simple, quick to set up
**Cons**: Key rotation required, less secure for production

### Option 2: Microsoft Entra ID (Recommended for Production)

```bash
# Don't set ANTHROPIC_FOUNDRY_API_KEY
# Azure SDK will use DefaultAzureCredential
```

**Prerequisites**:
- Assign **Azure AI User** or **Cognitive Services User** role to your identity
- Configure one of:
  - Azure CLI: `az login`
  - Managed Identity (in Azure VMs/containers)
  - Service Principal with client credentials

**Pros**: More secure, automatic credential rotation, audit logging
**Cons**: More complex setup

## Cost Tracking

Azure Foundry cost tracking via Azure Cost Management API is planned but not yet fully implemented in SHANDY.

**Current workaround**: View costs in Azure Portal:
1. Go to **Cost Management + Billing**
2. Filter by:
   - Service: "Azure AI Foundry" or "Cognitive Services"
   - Resource: Your Foundry resource name
   - Time range: Last 30 days

## Troubleshooting

### "baseURL and resource are mutually exclusive" Error

**Symptom**: Job fails with "API Error: baseURL and resource are mutually exclusive"

**Solution**:
1. Check your `.env` file - you have BOTH `ANTHROPIC_FOUNDRY_RESOURCE` and `ANTHROPIC_FOUNDRY_BASE_URL` set
2. Comment out `ANTHROPIC_FOUNDRY_BASE_URL` and only use `ANTHROPIC_FOUNDRY_RESOURCE`
3. Restart: `docker compose down && docker compose up -d`

**Note**: Claude Code requires you to set EITHER the resource name OR the base URL, not both.

### "No Authentication Configured" Error

**Symptom**: SHANDY shows "No Authentication Configured"

**Solution**:
1. Check `.env` has `CLAUDE_PROVIDER=foundry` and `CLAUDE_CODE_USE_FOUNDRY=1`
2. Verify `ANTHROPIC_FOUNDRY_RESOURCE` and `ANTHROPIC_FOUNDRY_API_KEY` are set
3. Restart SHANDY: `docker compose restart` or `uv run python -m shandy.web_app`

### 403 Forbidden Error

**Symptom**: API calls return 403 Forbidden

**Solutions**:
1. **Check subscription type**: Must be EA or MCA-E
2. **Verify permissions**: Need Contributor or Owner role on resource group
3. **For Entra ID**: Ensure **Cognitive Services User** role is assigned

### 404 Not Found Error

**Symptom**: API calls return 404

**Solutions**:
1. **Check resource name**: Must match exactly (case-sensitive)
2. **Verify region**: Must be East US 2 or Sweden Central
3. **Check deployment name**: Must match what you created in Azure portal

### 429 Rate Limit Error

**Symptom**: Too many requests error

**Solutions**:
1. Implement exponential backoff in your code
2. Reduce request frequency
3. Request quota increase: https://aka.ms/oai/stuquotarequest

### Subscription Not Eligible

**Symptom**: Error about subscription type not supported

**Solution**: Claude models require Enterprise Agreement or MCA-E subscriptions. Contact your Azure account manager about upgrading.

## Additional Resources

- [Microsoft Foundry Documentation](https://learn.microsoft.com/en-us/azure/ai-foundry/)
- [Claude in Azure Foundry Guide](https://learn.microsoft.com/en-us/azure/ai-foundry/foundry-models/how-to/use-foundry-models-claude)
- [Claude Code Foundry Documentation](https://code.claude.com/docs/en/microsoft-foundry)
- [Anthropic's Claude Models](https://www.anthropic.com/claude)
- [Azure Foundry Pricing](https://azure.microsoft.com/en-us/pricing/details/ai-foundry/)

## Notes

- **Data Residency**: Data is processed globally but stored in your resource's Azure geography
- **Model Versions**: Set version upgrade policy to "Once a new default version is available" for automatic updates
- **Content Filtering**: Use Azure AI Content Safety API - integrated filters not yet available for Claude
- **Preview Status**: Claude models are in preview - expect potential changes

## CloudBank-Specific Notes

If using CloudBank for academic research:

1. **Billing tracking**: CloudBank tracks usage at tag and service level
2. **No fund limit by default**: Inherits limits from your research fund
3. **Consult your PI**: For fine-grained usage tracking, contact your billing account manager
4. **Tag resources**: Consider tagging resources with your project/grant for better tracking
