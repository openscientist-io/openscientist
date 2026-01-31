# JGI Lakehouse Database Reference

## Table of Contents
- [GOLD Database](#gold-database)
- [IMG PostgreSQL Databases](#img-postgresql-databases)
- [IMG MySQL Databases](#img-mysql-databases)
- [Connection Examples](#connection-examples)

---

## GOLD Database

**Schema:** `gold-db-2 postgresql.gold`

Genomes OnLine Database - metadata about genome sequencing projects.

### Key Tables (42 total)

| Table | Description |
|-------|-------------|
| `study` | Research studies |
| `project` | Sequencing projects |
| `analysis_project` | Analysis/annotation projects |
| `organism` | Organisms being sequenced |
| `biosample` | Biological samples |
| `sequencing_project` | Sequencing runs |
| `contact` | Researchers and contacts |

### FK Relationships (31 total)

Notable relationships:
- `analysis_project.status_id` -> `cvap_status`
- `analysis_project.reference_ap_id` -> `analysis_project` (self-reference)
- `biosample.study_id` -> `study`
- `project.pi_id` -> `contact`

---

## IMG PostgreSQL Databases

All on `img-db-2 postgresql.*`

### img_core_v400 (244 tables, 141 FKs)

Core IMG database with gene, taxon, and annotation data.

Key tables:
- `gene` - Gene records
- `taxon` - Taxonomic information
- `scaffold` - Genome scaffolds
- `gene_cog_groups` - COG functional annotations
- `gene_pfam_families` - Pfam domain annotations

### img_ext (84 tables, 80 FKs)

Extended IMG data including:
- Pathway annotations
- Secondary metabolite clusters
- Comparative genomics data

### img_gold (72 tables, 14 FKs)

IMG-GOLD integration tables linking IMG taxons to GOLD metadata.

### img_sat_v450 (141 tables, 127 FKs)

IMG satellite database with:
- Experimental data
- Expression profiles
- Phenotype data

### img_sub (49 tables)

IMG submission system for new genome submissions.

### imgsg_dev (254 tables, 38 FKs)

IMG development database.

### Specialty Databases

| Schema | Tables | FKs | Description |
|--------|--------|-----|-------------|
| `img_i_taxon` | 8 | 0 | Taxonomy data |
| `img_methylome` | 10 | 9 | Methylome experiments |
| `img_proteome` | 15 | 7 | Proteomics data |
| `img_rnaseq` | 11 | 5 | RNA-seq experiments |

---

## IMG MySQL Databases

All on `img-db-1 mysql.*` (no FK metadata available)

| Schema | Tables | Description |
|--------|--------|-------------|
| `abc` | 18 | ABC transporter data |
| `img` | 5 | Core IMG tables |
| `imgvr_prod` | 7 | IMG/VR viral genomes |
| `mbin` | 17 | Metagenome binning |
| `misi` | 5 | Microbial signatures |

---

## Connection Examples

### Python - REST API

```python
from linkml_store import Client

client = Client()

# GOLD
gold_db = client.attach_database(
    "dremio-rest://lakehouse.jgi.lbl.gov?schema=gold-db-2 postgresql.gold"
)

# IMG Core
img_db = client.attach_database(
    "dremio-rest://lakehouse.jgi.lbl.gov?schema=img-db-2 postgresql.img_core_v400"
)
```

### CLI

```bash
# GOLD
linkml-store -d "dremio-rest://lakehouse.jgi.lbl.gov?schema=gold-db-2 postgresql.gold" ...

# IMG Core
linkml-store -d "dremio-rest://lakehouse.jgi.lbl.gov?schema=img-db-2 postgresql.img_core_v400" ...
```

### Direct SQL

```python
from linkml_store.api.stores.dremio_rest.dremio_rest_database import DremioRestDatabase

db = DremioRestDatabase("dremio-rest://lakehouse.jgi.lbl.gov")

# Query across schemas
df = db._execute_query('''
    SELECT g.study_name, COUNT(*) as project_count
    FROM "gold-db-2 postgresql".gold.study g
    JOIN "gold-db-2 postgresql".gold.project p ON g.study_id = p.study_id
    GROUP BY g.study_name
    ORDER BY project_count DESC
    LIMIT 20
''')
```
