#!/usr/bin/env bash
# Create a sanitized source archive from tracked Git files only.
#
# Uses `git archive`, so untracked local files (.env, .venv/, jobs/, data/, etc.)
# are never included. Run from the repository root.
#
# Usage:
#   tools/safe_archive.sh [REF] [OUTPUT]
#
# Examples:
#   tools/safe_archive.sh
#   tools/safe_archive.sh HEAD openscientist-source.tar.gz
#   tools/safe_archive.sh v1.0.0 /tmp/openscientist-v1.0.0.tar.gz

set -euo pipefail

REF="${1:-HEAD}"
SHORT_SHA="$(git rev-parse --short "$REF")"
OUTPUT="${2:-openscientist-source-${SHORT_SHA}.tar.gz}"

if ! git rev-parse --verify "$REF" >/dev/null 2>&1; then
  echo "error: unknown git ref: $REF" >&2
  exit 1
fi

git archive --format=tar.gz --output="$OUTPUT" "$REF"

echo "Created archive: $OUTPUT"
echo "Source ref:      $REF ($SHORT_SHA)"
echo "Contents:        tracked files only (no .env, .venv/, jobs/, or data/)"
