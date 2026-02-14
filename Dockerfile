# Dockerfile for SHANDY
# Builds on shandy-base which includes Python, Node.js, uv, and Claude CLI
FROM shandy-base:latest

# Build args
ARG SHANDY_COMMIT=unknown
ARG BUILD_TIME=unknown

# Install additional system dependencies (fonts for matplotlib)
RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI via npm (latest version for Azure Foundry support)
RUN npm install -g @anthropic-ai/claude-code

# Optionally install Phenix for structural biology
# Requires data/phenix-installer-*.tar.gz to be present
RUN if [ "$INSTALL_PHENIX" = "true" ]; then \
        INSTALLER=$(ls /tmp/phenix-installer-*.tar.gz 2>/dev/null | head -1) && \
        if [ -n "$INSTALLER" ]; then \
            cd /tmp && tar xzf "$INSTALLER" && \
            cd phenix-installer-* && ./install --prefix=/opt && \
            cd / && rm -rf /tmp/phenix-installer-*; \
        fi; \
    fi

# Set working directory
WORKDIR /app

# Copy package metadata files first
COPY pyproject.toml README.md alembic.ini uv.lock ./

# Copy source code (needed for editable install)
COPY src/ src/

# Install dependencies using uv
RUN uv pip install --system -e .

# Create jobs directory
RUN mkdir -p jobs

# Expose port for NiceGUI
EXPOSE 8080

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV SHANDY_COMMIT=${SHANDY_COMMIT}
ENV SHANDY_BUILD_TIME=${BUILD_TIME}
# Fixed path for GCP credentials (mounted via GCP_CREDENTIALS_FILE in docker-compose)
ENV GOOGLE_APPLICATION_CREDENTIALS=/app/gcp-credentials.json

# Run web app
# Note: In development mode, override with --reload flag by setting command in docker-compose.override.yml
CMD ["python", "-m", "shandy.web_app", "--host", "0.0.0.0", "--port", "8080", "--reload"]
