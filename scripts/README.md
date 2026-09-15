# Release Scripts

## create_release_archive.sh

Creates a clean release archive from git, excluding all `.gitignore`d files (virtual environments, build artifacts, etc.).

**Usage:**
```bash
# Create archive from HEAD
./scripts/create_release_archive.sh

# Create archive from a specific tag or commit
./scripts/create_release_archive.sh v1.0.0
```

This uses `git archive`, which automatically respects `.gitignore`, ensuring:
- `.venv/` is never included
- Local build artifacts are excluded
- `.env` files don't leak into releases
- Only committed source code is packaged

Archives are created in `./releases/` as `.tar.gz` files.
