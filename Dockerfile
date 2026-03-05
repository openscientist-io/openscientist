# Dockerfile for OpenScientist
# Builds on open_scientist-base which includes Python, Node.js, uv, and Claude CLI
FROM open_scientist-base:latest

# Build args
ARG OPEN_SCIENTIST_COMMIT=unknown
ARG BUILD_TIME=unknown

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

# Copy project files — deps already installed in base
COPY pyproject.toml README.md alembic.ini uv.lock ./
COPY src/ src/

# Create jobs directory
RUN mkdir -p jobs

# Expose port for NiceGUI
EXPOSE 8080

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV OPEN_SCIENTIST_COMMIT=${OPEN_SCIENTIST_COMMIT}
ENV OPEN_SCIENTIST_BUILD_TIME=${BUILD_TIME}
# Fixed path for GCP credentials (mounted via GCP_CREDENTIALS_FILE in docker-compose)
ENV GOOGLE_APPLICATION_CREDENTIALS=/app/gcp-credentials.json

CMD ["python", "-m", "open_scientist.web_app", "--host", "0.0.0.0", "--port", "8080"]
