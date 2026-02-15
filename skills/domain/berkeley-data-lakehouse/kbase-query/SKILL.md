---
name: kbase-query
description: Skills for querying the KBase/BERDL Datalake via the MCP REST API. Use this when users want to explore KBase databases, list tables, get schemas, sample data, or run SQL queries against the KBase data lake. Triggers on mentions of KBase, BERDL, or requests to query biological/microbiome data stored in KBase.
category: domain
---

# KBase Query

Query the KBase/BERDL Datalake MCP Server via REST API using Python.

## Setup

The `KBASE_TOKEN` environment variable must be set. Tokens expire after ~1 week.

## Python API Functions

Use `execute_code` with these helper functions to query the lakehouse:

```python
import os
import requests
import pandas as pd

KBASE_TOKEN = os.environ.get('KBASE_TOKEN')
BASE_URL = 'https://hub.berdl.kbase.us/apis/mcp'

def get_headers():
    return {
        'Authorization': f'Bearer {KBASE_TOKEN}',
        'Content-Type': 'application/json',
        'accept': 'application/json'
    }

def list_databases():
    '''List all available databases'''
    resp = requests.post(
        f'{BASE_URL}/delta/databases/list',
        headers=get_headers(),
        json={'use_hms': True, 'filter_by_namespace': True}
    )
    resp.raise_for_status()
    return resp.json()['databases']

def list_tables(database):
    '''List tables in a database'''
    resp = requests.post(
        f'{BASE_URL}/delta/databases/tables/list',
        headers=get_headers(),
        json={'database': database, 'use_hms': True}
    )
    resp.raise_for_status()
    return resp.json()['tables']

def query(sql, limit=100):
    '''Execute SQL query and return DataFrame'''
    resp = requests.post(
        f'{BASE_URL}/delta/tables/query',
        headers=get_headers(),
        json={'query': sql, 'limit': limit}
    )
    resp.raise_for_status()
    data = resp.json()
    rows = data.get('result', [])
    return pd.DataFrame(rows)
```

## Example Workflow

### 1. Explore available databases

```python
dbs = list_databases()
print(f"Available databases: {dbs}")
# → ['enigma_coral', 'nmdc_core', 'globalusers_kepangenome_parquet_1', ...]
```

### 2. List tables in a database

```python
tables = list_tables('nmdc_core')
print(f"Found {len(tables)} tables: {tables[:10]}")
# → ['annotation_terms_unified', 'cog_categories', 'kegg_ko_module', ...]
```

### 3. Query data with SQL

```python
# Simple query
df = query("SELECT * FROM nmdc_core.kegg_ko_module LIMIT 10")
print(df)

# Aggregation query
df = query("""
    SELECT module_id, COUNT(*) as ko_count
    FROM nmdc_core.kegg_ko_module
    GROUP BY module_id
    ORDER BY ko_count DESC
    LIMIT 20
""")
print(df)

# Join query (when needed)
df = query("""
    SELECT a.*, b.description
    FROM nmdc_core.kegg_ko_module a
    JOIN nmdc_core.kegg_modules b ON a.module_id = b.module_id
    LIMIT 10
""")
```

## Key Databases

| Database | Description |
|----------|-------------|
| `nmdc_core` | NMDC microbiome data (63 tables) |
| `globalusers_kepangenome_parquet_1` | Pangenomic data with GTDB taxonomy |
| `enigma_coral` | ENIGMA coral microbiome data |

## Key Tables in nmdc_core

| Table | Description |
|-------|-------------|
| `kegg_ko_module` | KEGG ortholog to module mappings |
| `cog_categories` | COG functional categories |
| `go_terms` | Gene Ontology terms |
| `ec_terms` | Enzyme Commission terms |
| `annotation_terms_unified` | Unified annotation data |

## API Reference

For complete endpoint documentation, see [references/api_reference.md](references/api_reference.md).

### Available Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/delta/databases/list` | POST | List databases |
| `/delta/databases/tables/list` | POST | List tables in database |
| `/delta/databases/tables/schema` | POST | Get table schema |
| `/delta/tables/query` | POST | Execute SQL query |
| `/delta/tables/sample` | POST | Sample rows (may timeout on large tables) |
| `/delta/tables/count` | POST | Get row count |

## Tips

- Use `query()` with SQL for most reliable results
- Large tables may timeout with `/delta/tables/sample` - use SQL with LIMIT instead
- SQL queries support standard operations: SELECT, JOIN, WHERE, GROUP BY, ORDER BY
- Maximum query limit is 1000 rows per request
