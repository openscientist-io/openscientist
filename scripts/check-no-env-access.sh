#!/bin/bash
# Check that os.getenv/os.environ is not used directly in the codebase.
# All environment variable access should go through get_settings() from shandy.settings.
#
# Exceptions:
#   - settings.py (the canonical source)
#   - Lines with "# noqa: env-ok" comment

set -e

# Find violations
violations=$(grep -rn "os\.getenv\|os\.environ" src/shandy --include="*.py" \
    | grep -v "settings\.py" \
    | grep -v "# noqa: env-ok" \
    || true)

if [ -n "$violations" ]; then
    echo "ERROR: Direct os.getenv/os.environ access found!"
    echo ""
    echo "Use get_settings() from shandy.settings instead."
    echo "If this is intentional, add '# noqa: env-ok' comment to the line."
    echo ""
    echo "Violations:"
    echo "$violations"
    exit 1
fi

exit 0
