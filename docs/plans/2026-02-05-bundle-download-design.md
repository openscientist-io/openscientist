# Bundle Download Feature Design

## Overview

Add a "Download Bundle" button that creates a `.tgz` archive containing the complete research package with RO-Crate metadata.

## Bundle Contents

```
{job_id}_bundle/
├── ro-crate-metadata.json    # RO-Crate manifest
├── final_report.pdf          # Main report
├── final_report.md           # Markdown source
├── config.json               # Job metadata
├── knowledge_graph.json      # Full knowledge state
├── data/
│   └── *.csv                 # Original uploaded data files
└── plots/
    ├── *.png                 # All visualizations
    └── *.json                # Plot metadata
```

Files included only if they exist (graceful handling of partial jobs).

## RO-Crate Metadata

Basic RO-Crate 1.1 compliant `ro-crate-metadata.json`:
- Lists all files with types and descriptions
- Plot descriptions pulled from companion `.json` metadata files
- Minimal provenance (dateCreated, creator tool reference)

## Implementation

### New Module: `src/shandy/bundle_generator.py`

- `create_bundle(job_dir: Path) -> bytes` - creates tgz in memory
- `generate_ro_crate_metadata(job_dir: Path) -> dict` - builds RO-Crate JSON

### UI Change: `src/shandy/web_app.py`

Add third button in Report tab (after existing PDF button):

```python
ui.button(
    "Download Bundle",
    on_click=lambda: download_bundle(job_id),
    icon="folder_zip"
).props("color=secondary")
```

### Dependencies

None - uses stdlib `tarfile` and `json`.
