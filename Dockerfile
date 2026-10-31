# Dockerfile for OpenScientist
# Builds on openscientist-base, which carries Python, uv, the locked
# dependencies and the WeasyPrint libraries

FROM openscientist-base:latest

# Build args
ARG OPENSCIENTIST_COMMIT=unknown
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

# Reinstall the project so the web image has dependencies added since the base
# image was built. That delta comes from the lock, not a fresh resolve.
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt \
        -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt \
    && uv pip install --system --no-deps -e . \
    && rm /tmp/requirements.txt

# Create jobs directory
RUN mkdir -p jobs

# Expose port for NiceGUI
EXPOSE 8080

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV OPENSCIENTIST_COMMIT=${OPENSCIENTIST_COMMIT}
ENV OPENSCIENTIST_BUILD_TIME=${BUILD_TIME}
# Fixed path for GCP credentials (mounted via GCP_CREDENTIALS_FILE in docker-compose)
ENV GOOGLE_APPLICATION_CREDENTIALS=/app/gcp-credentials.json

CMD ["python", "-m", "openscientist.web_app", "--host", "0.0.0.0", "--port", "8080"]
