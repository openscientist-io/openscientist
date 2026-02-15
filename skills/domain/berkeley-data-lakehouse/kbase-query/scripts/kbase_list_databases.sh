#!/bin/bash
# List all databases in KBase
# Usage: kbase_list_databases.sh

: "${KBASE_TOKEN:?KBASE_TOKEN environment variable required}"
: "${KBASE_MCP_URL:=https://hub.berdl.kbase.us/apis/mcp}"

curl -s -X POST "${KBASE_MCP_URL}/delta/databases/list" -H "accept: application/json" -H "Authorization: Bearer ${KBASE_TOKEN}" -H "Content-Type: application/json" -d '{"use_hms": true, "filter_by_namespace": true}' | jq .
