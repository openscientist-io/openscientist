# Dockerfile for SHANDY
# Builds on shandy-base which includes Python, Node.js, uv, and Claude CLI
FROM shandy-base:latest

# Build args
ARG SHANDY_COMMIT=unknown
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
ENV SHANDY_COMMIT=${SHANDY_COMMIT}
ENV SHANDY_BUILD_TIME=${BUILD_TIME}
# Fixed path for GCP credentials (mounted via GCP_CREDENTIALS_FILE in docker-compose)
ENV GOOGLE_APPLICATION_CREDENTIALS=/app/gcp-credentials.json

# Run web app (--reload only in dev mode)
CMD python -m shandy.web_app --host 0.0.0.0 --port 8080 ${SHANDY_DEV_MODE:+--reload}
