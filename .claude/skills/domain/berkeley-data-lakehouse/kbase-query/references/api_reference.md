# KBase MCP API Reference

## Table of Contents
- [Health Check](#health-check)
- [Database Operations](#database-operations)
- [Table Data Operations](#table-data-operations)

---

## Health Check

### GET /health

Returns health status of all backend services.

**Request:** No body required.

**Response:** `DeepHealthResponse`

---

## Database Operations

### POST /delta/databases/list

Lists all databases in Hive metastore.

**Request Body:**
```json
{
  "use_hms": true,
  "filter_by_namespace": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| use_hms | boolean | No | Use Hive metastore |
| filter_by_namespace | boolean | No | Filter by namespace |

---

### POST /delta/databases/tables/list

Lists tables within a specific database.

**Request Body:**
```json
{
  "database": "my_database",
  "use_hms": true
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| database | string | **Yes** | Database name |
| use_hms | boolean | No | Use Hive metastore |

---

### POST /delta/databases/tables/schema

Retrieves column names and types for a table.

**Request Body:**
```json
{
  "database": "my_database",
  "table": "my_table"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| database | string | **Yes** | Database name |
| table | string | **Yes** | Table name |

---

### POST /delta/databases/structure

Complete structure of all databases, optionally with schemas.

**Request Body:**
```json
{
  "use_hms": true,
  "with_schema": false
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| use_hms | boolean | No | Use Hive metastore |
| with_schema | boolean | No | Include table schemas |

---

## Table Data Operations

### POST /delta/tables/count

Gets row count for a table.

**Request Body:**
```json
{
  "database": "my_database",
  "table": "my_table"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| database | string | **Yes** | Database name |
| table | string | **Yes** | Table name |

---

### POST /delta/tables/sample

Retrieves sample rows from a table.

**Request Body:**
```json
{
  "database": "my_database",
  "table": "my_table",
  "limit": 10,
  "columns": ["col1", "col2"],
  "where_clause": "col1 > 100"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| database | string | **Yes** | Database name |
| table | string | **Yes** | Table name |
| limit | integer | No | Max rows (default varies, max 100) |
| columns | array | No | Specific columns to return |
| where_clause | string | No | SQL WHERE condition |

---

### POST /delta/tables/query

Executes SQL queries with pagination.

**Request Body:**
```json
{
  "query": "SELECT * FROM db.table WHERE col = 'value'",
  "limit": 100,
  "offset": 0
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| query | string | **Yes** | SQL query |
| limit | integer | No | Max rows (default 100, max 1000) |
| offset | integer | No | Skip N rows for pagination |

**Example queries:**
```sql
-- Basic select
SELECT * FROM mydb.mytable LIMIT 50

-- Aggregation
SELECT species, COUNT(*) as cnt FROM mydb.organisms GROUP BY species

-- Join
SELECT a.*, b.annotation
FROM mydb.genes a
JOIN mydb.annotations b ON a.gene_id = b.gene_id
```

---

### POST /delta/tables/select

Builds structured SELECT queries with joins, aggregations, and filtering.

**Request Body:**
```json
{
  "database": "my_database",
  "table": "my_table",
  "columns": ["col1", "col2"],
  "joins": [...],
  "filters": [...],
  "group_by": ["col1"],
  "order_by": ["col1 ASC"],
  "aggregations": [...],
  "limit": 100,
  "offset": 0
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| database | string | **Yes** | Database name |
| table | string | **Yes** | Table name |
| columns | array | No | Columns to select |
| joins | array | No | Join specifications |
| filters | array | No | Filter conditions |
| group_by | array | No | GROUP BY columns |
| order_by | array | No | ORDER BY expressions |
| aggregations | array | No | Aggregation functions |
| limit | integer | No | Max rows |
| offset | integer | No | Skip N rows |

For complex queries, prefer using `/delta/tables/query` with raw SQL.
