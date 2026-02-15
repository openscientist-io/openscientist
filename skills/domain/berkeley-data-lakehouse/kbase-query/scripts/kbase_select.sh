#!/bin/bash
# Structured select query
# Usage: kbase_select.sh <database> <table> [limit]

: "${KBASE_TOKEN:?KBASE_TOKEN environment variable required}"
: "${KBASE_MCP_URL:=https://hub.berdl.kbase.us/apis/mcp}"

database="${1:?Usage: kbase_select.sh <database> <table> [limit]}"
table="${2:?Usage: kbase_select.sh <database> <table> [limit]}"
limit="${3:-100}"
payload=$(jq -n --arg db "$database" --arg tbl "$table" --argjson lim "$limit" '{"database": $db, "table": $tbl, "limit": $lim}')

curl -s -X POST "${KBASE_MCP_URL}/delta/tables/select" -H "accept: application/json" -H "Authorization: Bearer ${KBASE_TOKEN}" -H "Content-Type: application/json" -d "$payload" | jq .
