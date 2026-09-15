# Dockerfile for OpenScientist
# Builds on openscientist-base which includes Python, Node.js, uv, and Claude CLI

# The pinned codex binary, built separately by Dockerfile.codex. Declared as a
# stage because the classic builder does not expand build args inside
# 'COPY --from=', which fails with "invalid reference format".
ARG CODEX_IMAGE=acrcbraindev.azurecr.io/openscientist-codex:8f8009fc
FROM ${CODEX_IMAGE} AS codex-bin

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

# WeasyPrint system libraries (libpango/cairo/gdk-pixbuf/glib). Needed so any
# web-side PDF rendering matches the agent's WeasyPrint output rather than
# falling back to fpdf2. Mirrors the agent image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpango-1.0-0 \
    libpangocairo-1.0-0 \
    libgdk-pixbuf-2.0-0 \
    libcairo2 \
    libffi8 \
    shared-mime-info \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Install the open-codex CLI. Rather than recompiling it from Rust here (a heavy
# codex-rs release build that OOMs / hits the 1h timeout on ACR's default
# Basic-tier build agent), copy the identical pinned binary already built into
# the agent image (Dockerfile.agent, same CODEX_REF). When CODEX_REF changes,
# rebuild openscientist-agent (on a host with enough RAM); this image and every
# deploy then just copy the prebuilt binary — no Rust toolchain in the web build.
COPY --from=codex-bin /usr/local/bin/codex /usr/local/bin/codex
RUN chmod +x /usr/local/bin/codex

# Copy project files — deps already installed in base
COPY pyproject.toml README.md alembic.ini uv.lock ./
COPY src/ src/
# web_app resolves BUILTIN_SKILLS_DIR to /app/skills. Without this the
# built-in source fails every boot with 'Path does not exist: /app/skills'
# and the bundled skills are silently missing from every environment.
COPY skills/ skills/

# Reinstall the project so the web image has dependencies added since the base
# image was built, notably the openai-codex SDK used by the codex agent path
# (in-page chat + discovery). The pyproject override drops the musl-only
# openai-codex-cli-bin. The codex binary itself is provisioned above. That delta
# comes from the lock, not a fresh resolve.
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt \
        -o /tmp/requirements.txt \
    && uv pip install --system -r /tmp/requirements.txt \
    && uv pip install --system --no-deps -e . \
    && rm /tmp/requirements.txt

RUN groupadd --gid 1001 openscientist \
    && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash openscientist

RUN mkdir -p jobs .nicegui \
    && chown -R openscientist:openscientist jobs .nicegui

# Expose port for NiceGUI
EXPOSE 8080

# Set environment variables
ENV PYTHONUNBUFFERED=1
ENV OPENSCIENTIST_COMMIT=${OPENSCIENTIST_COMMIT}
ENV OPENSCIENTIST_BUILD_TIME=${BUILD_TIME}
# Fixed path for GCP credentials (mounted via GCP_CREDENTIALS_FILE in docker-compose)
ENV GOOGLE_APPLICATION_CREDENTIALS=/app/gcp-credentials.json

USER openscientist

CMD ["python", "-m", "openscientist.web_app", "--host", "0.0.0.0", "--port", "8080"]
