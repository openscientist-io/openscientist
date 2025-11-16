# Dockerfile for SHANDY
FROM python:3.11-slim

# Install system dependencies including Node.js
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI via npm (pinned to v2.0.37 - last version before beta header)
RUN npm install -g @anthropic-ai/claude-code@2.0.37

# Set working directory
WORKDIR /app

# Copy package metadata files first
COPY pyproject.toml README.md ./

# Copy source code (needed for editable install)
COPY src/ src/

# Install dependencies
RUN pip install --no-cache-dir uv && \
    uv pip install --system -e .

# Copy additional application files
COPY .claude/ .claude/
COPY CLAUDE.md .

# Create jobs directory
RUN mkdir -p jobs

# Expose port for NiceGUI
EXPOSE 8080

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV ANTHROPIC_AUTH_TOKEN=""
ENV DISABLE_AUTH="false"
ENV APP_PASSWORD_HASH=""

# Run web app
CMD ["python", "-m", "shandy.web_app", "--host", "0.0.0.0", "--port", "8080"]
