#!/bin/bash
# Check KBase MCP API health
# Usage: kbase_health.sh

: "${KBASE_TOKEN:?KBASE_TOKEN environment variable required}"
: "${KBASE_MCP_URL:=https://hub.berdl.kbase.us/apis/mcp}"

curl -s -X GET "${KBASE_MCP_URL}/health" -H "accept: application/json" -H "Authorization: Bearer ${KBASE_TOKEN}" | jq .
