#!/bin/bash
# Get table schema (columns)
# Usage: kbase_table_schema.sh <database> <table>

: "${KBASE_TOKEN:?KBASE_TOKEN environment variable required}"
: "${KBASE_MCP_URL:=https://hub.berdl.kbase.us/apis/mcp}"

database="${1:?Usage: kbase_table_schema.sh <database> <table>}"
table="${2:?Usage: kbase_table_schema.sh <database> <table>}"
payload=$(jq -n --arg db "$database" --arg tbl "$table" '{"database": $db, "table": $tbl}')

curl -s -X POST "${KBASE_MCP_URL}/delta/databases/tables/schema" -H "accept: application/json" -H "Authorization: Bearer ${KBASE_TOKEN}" -H "Content-Type: application/json" -d "$payload" | jq .
