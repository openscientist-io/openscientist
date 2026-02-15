#!/bin/bash
# Sample rows from a table
# Usage: kbase_table_sample.sh <database> <table> [limit]

: "${KBASE_TOKEN:?KBASE_TOKEN environment variable required}"
: "${KBASE_MCP_URL:=https://hub.berdl.kbase.us/apis/mcp}"

database="${1:?Usage: kbase_table_sample.sh <database> <table> [limit]}"
table="${2:?Usage: kbase_table_sample.sh <database> <table> [limit]}"
limit="${3:-10}"
payload=$(jq -n --arg db "$database" --arg tbl "$table" --argjson lim "$limit" '{"database": $db, "table": $tbl, "limit": $lim}')

curl -s -X POST "${KBASE_MCP_URL}/delta/tables/sample" -H "accept: application/json" -H "Authorization: Bearer ${KBASE_TOKEN}" -H "Content-Type: application/json" -d "$payload" | jq .
