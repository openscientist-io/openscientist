#!/bin/bash
# List tables in a database
# Usage: kbase_list_tables.sh <database>

: "${KBASE_TOKEN:?KBASE_TOKEN environment variable required}"
: "${KBASE_MCP_URL:=https://hub.berdl.kbase.us/apis/mcp}"

database="${1:?Usage: kbase_list_tables.sh <database>}"
payload=$(jq -n --arg db "$database" '{"use_hms": true, "database": $db}')

curl -s -X POST "${KBASE_MCP_URL}/delta/databases/tables/list" -H "accept: application/json" -H "Authorization: Bearer ${KBASE_TOKEN}" -H "Content-Type: application/json" -d "$payload" | jq .
