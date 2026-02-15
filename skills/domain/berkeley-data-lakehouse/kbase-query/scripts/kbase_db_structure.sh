#!/bin/bash
# Get complete database structure
# Usage: kbase_db_structure.sh [with_schema: true|false]

: "${KBASE_TOKEN:?KBASE_TOKEN environment variable required}"
: "${KBASE_MCP_URL:=https://hub.berdl.kbase.us/apis/mcp}"

with_schema="${1:-false}"
payload=$(jq -n --argjson ws "$with_schema" '{"use_hms": true, "with_schema": $ws}')

curl -s -X POST "${KBASE_MCP_URL}/delta/databases/structure" -H "accept: application/json" -H "Authorization: Bearer ${KBASE_TOKEN}" -H "Content-Type: application/json" -d "$payload" | jq .
