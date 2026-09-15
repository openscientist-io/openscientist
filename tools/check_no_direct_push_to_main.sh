#!/usr/bin/env bash
# Pre-push hook: block direct pushes to main.
# Installed via .pre-commit-config.yaml (stage: pre-push).
# Git passes pushed refs on stdin as: <local ref> <local sha1> <remote ref> <remote sha1>
set -euo pipefail

blocked=0
while read -r _local_ref _local_sha remote_ref _remote_sha; do
    if [[ "$remote_ref" == "refs/heads/main" ]]; then
        blocked=1
    fi
done

if [[ "$blocked" -eq 1 ]]; then
    echo "❌ Direct pushes to main are not allowed. Create a feature branch." >&2
    exit 1
fi

exit 0
