# Phenix Setup for Open Scientist

Open Scientist supports Phenix integration for structural biology analyses. Phenix is **optional** - Open Scientist works fine without it, but you won't have access to structural biology tools.

## What is Phenix?

Phenix is a comprehensive software suite for macromolecular crystallography, providing tools for structure determination, refinement, and validation. Open Scientist uses Phenix for:

- Structure comparison and alignment (phenix.superpose_pdbs)
- Validation metrics (phenix.clashscore, phenix.cablam_validate)
- Chain comparison (phenix.chain_comparison)
- AlphaFold confidence analysis (custom parsing)

## Do I Need Phenix?

**You need Phenix if:**
- Analyzing protein/nucleic acid structures (PDB/mmCIF files)
- Comparing experimental vs predicted structures
- Running crystallographic refinement validation
- Working with structural biology data

**You DON'T need Phenix if:**
- Only analyzing metabolomics, genomics, or tabular data
- Not working with macromolecular structures
- Just want to test Open Scientist's general discovery capabilities

## Installation Options

### Option 1: Docker Deployment (Recommended for Production)

Phenix is pre-installed in the Docker image. You just need to provide the installer file during build.

#### Prerequisites

1. **Download Phenix installer** (one-time, not in git):
   ```bash
   # Download from Phenix website
   # https://phenix-online.org/download/
   # Get: phenix-installer-1.21.2-5419-intel-linux-2.6-x86_64-centos6.tar.gz
   ```

2. **Place installer in `data/` directory**:
   ```bash
   # Create data directory if needed
   mkdir -p data

   # Move the .tar.gz file to data/ directory
   mv phenix-installer-1.21.2-5419-intel-linux-2.6-x86_64-centos6.tar.gz data/

   # Verify placement
   ls -lh data/phenix-installer-*.tar.gz
   # Should show ~3GB file
   ```

   **Why `data/`?** Keeps project root clean and follows convention that `data/` contains large binary files (gitignored but accessible to Docker build).

3. **Build Docker image**:
   ```bash
   # For local testing (M1/M2 Mac):
   docker compose up --build

   # For deployment to x86_64 Linux server:
   docker build --platform linux/amd64 -t open_scientist .
   ```

#### Deployment to GASSH

```bash
# 1. Transfer installer to server (one-time)
rsync -avz --progress \
  phenix-installer-1.21.2-5419-intel-linux-2.6-x86_64-centos6.tar.gz \
  gassh:~/open_scientist/data/

# 2. Deploy normally
make deploy
```

### Option 2: Local Development (macOS/Linux)

For local development without Docker:

#### macOS Installation

1. **Download Phenix for macOS**:
   - Visit https://phenix-online.org/download/
   - Get: `phenix-installer-1.21.2-5419-intel-mac-osx-10.6.tar.gz`

2. **Install**:
   ```bash
   cd ~/Downloads
   tar xzf phenix-installer-1.21.2-5419-intel-mac-osx-10.6.tar.gz
   cd phenix-installer-1.21.2-5419-intel-mac-osx-10.6
   ./install --prefix=/Applications
   ```

3. **Configure Open Scientist**:
   ```bash
   # Add to .env
   echo "PHENIX_PATH=/Applications/phenix-1.21.2-5419" >> .env
   ```

4. **Test**:
   ```bash
   source /Applications/phenix-1.21.2-5419/phenix_env.sh
   phenix.python --version
   ```

#### Linux Installation

1. **Download Phenix for Linux**:
   - Visit https://phenix-online.org/download/
   - Get: `phenix-installer-1.21.2-5419-intel-linux-2.6-x86_64-centos6.tar.gz`

2. **Install**:
   ```bash
   cd ~/Downloads
   tar xzf phenix-installer-1.21.2-5419-intel-linux-2.6-x86_64-centos6.tar.gz
   cd phenix-installer-1.21.2-5419-intel-linux-2.6-x86_64-centos6
   ./install --prefix=/opt
   ```

3. **Configure Open Scientist**:
   ```bash
   # Add to .env
   echo "PHENIX_PATH=/opt/phenix-1.21.2-5419" >> .env
   ```

## Verification

After installation, verify Phenix is working:

### Docker

```bash
# Check Phenix installed in container
docker exec open_scientist-open_scientist-1 test -f /opt/phenix-1.21.2-5419/phenix_env.sh && \
  echo "✅ Phenix installed" || echo "❌ Phenix NOT found"

# Check Open Scientist detects Phenix
docker exec open_scientist-open_scientist-1 python -c "
import os
os.environ['PHENIX_PATH'] = '/opt/phenix-1.21.2-5419'
from open_scientist.phenix_setup import check_phenix_available
print('✅ Phenix available' if check_phenix_available() else '❌ Phenix not available')
"

# Check MCP server registers Phenix tools
docker logs open_scientist-open_scientist-1 2>&1 | grep -i phenix
# Should see: "✅ Phenix tools registered"
```

### Local Development

```bash
# Check environment variable
echo $PHENIX_PATH
# Should show: /Applications/phenix-1.21.2-5419 (or /opt/...)

# Test Phenix setup
python -c "
from open_scientist.phenix_setup import check_phenix_available
print('✅ Phenix available' if check_phenix_available() else '❌ Phenix not available')
"
```

## Running Without Phenix

Open Scientist gracefully handles missing Phenix:

- **MCP server startup**: Logs "⚠️ Phenix tools not available" but continues
- **Tool availability**: Phenix tools (run_phenix_tool, compare_structures, parse_alphafold_confidence) won't be registered
- **Job execution**: Agent can still use execute_code, search_pubmed, update_knowledge_state
- **Structural biology jobs**: Will use BioPython/Python-based analysis instead of Phenix

## Troubleshooting

### "PHENIX_PATH not configured"

**Solution**: Add to `.env`:
```bash
PHENIX_PATH=/Applications/phenix-1.21.2-5419  # macOS
# or
PHENIX_PATH=/opt/phenix-1.21.2-5419           # Linux/Docker
```

### "phenix_env.sh not found"

**Check installation**:
```bash
ls -la $PHENIX_PATH/phenix_env.sh
```

**Solution**: Verify PHENIX_PATH points to correct directory containing `phenix_env.sh`

### "Phenix tools not registered" in logs

**Check**:
```bash
# Verify installation exists
test -f /opt/phenix-1.21.2-5419/phenix_env.sh && echo "Found" || echo "Missing"

# Check environment variable is set
docker exec open_scientist-open_scientist-1 env | grep PHENIX_PATH
```

### Docker build fails: "No such file"

**Error**: `COPY data/phenix-installer-*.tar.gz /tmp/`

**Solution**: Download installer and place in `data/` directory before building:
```bash
mkdir -p data
mv phenix-installer-*.tar.gz data/
```

### glibc version incompatibility (rare)

**Symptom**: Phenix binaries fail to execute on Ubuntu 24.04

**Solution**: Use Ubuntu 20.04 base image:
```dockerfile
FROM ubuntu:20.04
# Install Python 3.11 manually
```

## File Sizes

- Phenix installer: ~3.0 GB (download)
- Installed Phenix: ~10-15 GB (on disk)
- Docker image with Phenix: ~8-10 GB

## Security Notes

- Phenix installer contains pre-compiled binaries
- Downloaded from official Phenix website
- Not included in git repository (too large, licensed software)
- Each deployment needs to download/transfer installer separately

## License

Phenix is free for academic use. See https://phenix-online.org/license/ for details.

## Getting Help

**Phenix installation issues**: https://phenix-online.org/documentation/install-setup-run.html

**Open Scientist Phenix integration issues**: Check GitHub issues or deployment logs
