# Running SHANDY Locally (for Phenix development)

Since your Mac is ARM64 and Phenix requires x86_64, running SHANDY locally allows you to use the macOS Phenix installation.

## Setup

1. Install dependencies:
```bash
uv sync
```

2. Configure .env for local use:
```bash
# In .env, set:
PHENIX_PATH=/Applications/phenix-1.21.2-5419
PORT=8080
```

3. Run the web app:
```bash
uv run python -m shandy.web_app
```

## For Production Deployment

The Docker setup with x86_64 Phenix will work on x86_64 servers (like gassh).
Use `make deploy` to deploy to production.
