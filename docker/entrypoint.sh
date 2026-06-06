#!/bin/sh
# Entrypoint for the OpenScientist web container.
#
# Runs database migrations before launching the server so a fresh clone /
# `docker compose up` comes up with a complete schema (issue #134). The postgres
# dependency uses `condition: service_healthy`, so the DB is reachable by the
# time this runs. Migrations are idempotent (`upgrade head` on an already-current
# DB is a no-op) and the deployment is single-replica, so there is no
# leader-election concern.
#
# Migrations run ONLY for the default server command (argv starts with
# `python ...`). One-off invocations such as `docker compose run openscientist
# alembic ...`, `... bash`, or `make reset-db` pass a different command and are
# left untouched. Set OPENSCIENTIST_SKIP_MIGRATIONS=true to let an operator own
# migration timing (e.g. run them manually before flipping production traffic).
set -e

if [ "$1" = "python" ] && [ "${OPENSCIENTIST_SKIP_MIGRATIONS:-false}" != "true" ]; then
    echo "Running alembic migrations..."
    alembic upgrade head
fi

exec "$@"
