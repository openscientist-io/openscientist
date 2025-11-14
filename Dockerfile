# Dockerfile for SHANDY
FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
# Note: This assumes Claude Code CLI is available via curl
# Update this URL when official installation method is available
RUN curl -fsSL https://download.anthropic.com/claude-code/install.sh | bash || \
    echo "Claude Code CLI not available yet - will need manual installation"

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

# Run web app
CMD ["python", "-m", "shandy.web_app", "--host", "0.0.0.0", "--port", "8080"]
