# Running SHANDY Locally

This guide explains how to run SHANDY locally (outside Docker) for development.

## Why Run Locally?

- **Faster iteration**: No Docker rebuild needed for code changes
- **Native Phenix**: Use macOS Phenix installation (M1/M2 Macs can't run x86_64 Phenix in Docker)
- **Development**: Easier debugging and testing

## Prerequisites

- Python 3.10+
- `uv` package manager
- Claude Code CLI installed
- One of:
  - **CBORG** API token
  - **Vertex AI** GCP project configured (see `docs/VERTEX_SETUP.md`)
- *Optional*: Phenix installation (for structural biology features)

## Setup

### 1. Install Dependencies

```bash
# Install SHANDY package and dependencies
uv pip install -e .
```

### 2. Install Claude Code CLI

If not already installed:

```bash
# Install via npm
npm install -g @anthropic-ai/claude-code

# Verify installation
claude --version
```

### 3. Configure Environment

Create `.env` file with your provider configuration:

#### Option A: CBORG

```bash
# Provider
CLAUDE_PROVIDER=cborg
ANTHROPIC_AUTH_TOKEN=sk-ant-your-token-here
ANTHROPIC_BASE_URL=https://api.cborg.lbl.gov

# Web app
PORT=8080
STORAGE_SECRET=your-secret-here
DISABLE_AUTH=false
APP_PASSWORD_HASH=$2b$12$H77l168RKZRcRlElvBqr.O7UZxarzIZU9LUfPDsHvb/DmeekUUKZa
```

#### Option B: Vertex AI

```bash
# Provider
CLAUDE_PROVIDER=vertex
ANTHROPIC_VERTEX_PROJECT_ID=your-gcp-project
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
CLOUD_ML_REGION=us-east5
GCP_BILLING_ACCOUNT_ID=XXXXXX-YYYYYY-ZZZZZZ

# Web app
PORT=8080
STORAGE_SECRET=your-secret-here
DISABLE_AUTH=false
```

See `.env.example` for all available options.

### 4. Optional: Configure Phenix

If you have Phenix installed locally (macOS/Linux):

```bash
# Add to .env
PHENIX_PATH=/Applications/phenix-1.21.2-5419  # macOS
# or
PHENIX_PATH=/opt/phenix-1.21.2-5419           # Linux
```

See `docs/PHENIX_SETUP.md` for Phenix installation instructions.

## Running the Application

### Start Web App

```bash
# Run web interface
python -m shandy.web_app

# Or with uv:
uv run python -m shandy.web_app
```

Access at: http://localhost:8080

### Run Job Manager CLI

```bash
# List jobs
python -m shandy.job_manager list

# Get job details
python -m shandy.job_manager get <job_id>

# Cleanup old jobs
python -m shandy.job_manager cleanup --days 7

# View job summary
python -m shandy.job_manager summary
```

### Run Orchestrator Directly

For debugging/testing the discovery loop:

```bash
# Run discovery for existing job
python -m shandy.orchestrator --job-dir jobs/job_xxx
```

## Development Workflow

1. **Make code changes** - Edit Python files
2. **Restart app** - Stop (Ctrl+C) and restart `python -m shandy.web_app`
3. **Test** - Submit job via web UI or CLI
4. **Check logs** - View console output for errors
5. **Iterate** - Repeat!

No Docker rebuild needed - changes are immediate.

## Differences from Docker Deployment

| Aspect | Local | Docker |
|--------|-------|--------|
| **Code changes** | Immediate | Requires `make rebuild` |
| **Phenix** | Native macOS/Linux | x86_64 only (Linux container) |
| **Dependencies** | Manual install (`uv`) | Pre-installed in image |
| **Port** | 8080 (configurable) | 8080 (docker-compose.yml) |
| **Data persistence** | Local `jobs/` directory | Docker volume |
| **Production** | Not recommended | Recommended |

## Switching Back to Docker

```bash
# Stop local app (Ctrl+C in terminal)

# Start Docker containers
docker-compose up -d
```

## Troubleshooting

### "Module not found" errors

**Solution**: Reinstall dependencies:
```bash
uv pip install -e .
```

### "claude: command not found"

**Solution**: Install Claude Code CLI:
```bash
npm install -g @anthropic-ai/claude-code
```

### Port 8080 already in use

**Solution**: Either stop Docker containers or change port in `.env`:
```bash
# Stop Docker
docker-compose down

# Or change port
echo "PORT=8081" >> .env
```

### Provider authentication errors

**Solution**: Verify your provider configuration in `.env`:
```bash
# For CBORG: Check token is valid
curl -H "Authorization: Bearer $ANTHROPIC_AUTH_TOKEN" \
  https://api.cborg.lbl.gov/key/info

# For Vertex: Test service account
gcloud auth activate-service-account \
  --key-file=$GOOGLE_APPLICATION_CREDENTIALS
```

## For Production Deployment

Use Docker deployment for production:

```bash
# Build and deploy to server
make deploy

# Or build locally for testing
make build
make start
```

Docker provides:
- Consistent environment across deployments
- Pre-installed dependencies (Phenix, Claude CLI, etc.)
- Easy rollback and versioning
- Resource isolation
