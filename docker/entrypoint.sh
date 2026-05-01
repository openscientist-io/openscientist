#!/bin/sh
# OpenScientist app container entrypoint.
#
# Runs Alembic migrations to head before launching the FastAPI server, so a
# fresh `docker compose up -d` brings up a fully working app without the
# operator having to remember a separate `alembic upgrade head` step.
#
# Migrations are idempotent — running `upgrade head` against an already-current
# DB is a no-op, so this is safe to run on every container start.
#
# Set OPENSCIENTIST_SKIP_MIGRATIONS=true in .env to skip the migration step
# (e.g., when a DBA is managing migration timing manually for a production deploy).
set -e

if [ "${OPENSCIENTIST_SKIP_MIGRATIONS:-false}" = "true" ]; then
  echo "[entrypoint] Skipping alembic migrations (OPENSCIENTIST_SKIP_MIGRATIONS=true)"
else
  echo "[entrypoint] Running alembic upgrade head..."
  uv run alembic upgrade head
fi

echo "[entrypoint] Starting OpenScientist web app..."
exec python -m openscientist.web_app --host 0.0.0.0 --port 8080
