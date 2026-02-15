#!/bin/bash
# Execute SQL query
# Usage: kbase_query.sh <sql_query> [limit]

: "${KBASE_TOKEN:?KBASE_TOKEN environment variable required}"
: "${KBASE_MCP_URL:=https://hub.berdl.kbase.us/apis/mcp}"

query="${1:?Usage: kbase_query.sh <sql_query> [limit]}"
limit="${2:-100}"
payload=$(jq -n --arg q "$query" --argjson lim "$limit" '{"query": $q, "limit": $lim}')

curl -s -X POST "${KBASE_MCP_URL}/delta/tables/query" -H "accept: application/json" -H "Authorization: Bearer ${KBASE_TOKEN}" -H "Content-Type: application/json" -d "$payload" | jq .
