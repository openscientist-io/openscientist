#!/usr/bin/env bash
#
# Download the Exomiser CLI + data bundle to a host directory, for bind-mounting
# read-only into OpenScientist agent containers (EXOMISER_HOST_PATH). The data is
# NOT baked into any Docker image — only the Java runtime is. See
# notes/exomiser-integration-plan.md.
#
# Usage:
#   scripts/download-exomiser.sh
#   EXOMISER_DATA_VERSION=2406 EXOMISER_ASSEMBLY=hg38 EXOMISER_DIR=/data/exomiser \
#     scripts/download-exomiser.sh
#
# URLs confirmed against github.com/exomiser/Exomiser/releases and
# data.monarchinitiative.org/exomiser/latest (2026-05).
set -euo pipefail

EXOMISER_VERSION="${EXOMISER_VERSION:-15.0.0}"          # CLI version (GitHub releases)
EXOMISER_DATA_VERSION="${EXOMISER_DATA_VERSION:-2512}"  # Monarch data bundle (YYMM); 2512 pairs with CLI 15.x
EXOMISER_ASSEMBLY="${EXOMISER_ASSEMBLY:-hg38}"          # hg19 | hg38
EXOMISER_DIR="${EXOMISER_DIR:-./exomiser}"
# Use the immutable, versioned /data/ path (NOT /latest/, which lags at 2406+CLI14).
DATA_BASE="${EXOMISER_DATA_BASE:-https://data.monarchinitiative.org/exomiser/data}"
CLI_URL="${EXOMISER_CLI_URL:-https://github.com/exomiser/Exomiser/releases/download/${EXOMISER_VERSION}/exomiser-cli-${EXOMISER_VERSION}-distribution.zip}"

INSTALL_DIR="${EXOMISER_DIR}/exomiser-cli-${EXOMISER_VERSION}"
STAGING="${EXOMISER_DIR}/.staging"
CLI_ZIP="exomiser-cli-${EXOMISER_VERSION}-distribution.zip"
DATA_ZIP="${EXOMISER_DATA_VERSION}_${EXOMISER_ASSEMBLY}.zip"
PHENO_ZIP="${EXOMISER_DATA_VERSION}_phenotype.zip"

log() { printf '\n>>> %s\n' "$*"; }

command -v curl >/dev/null || { echo "ERROR: curl is required"; exit 1; }
command -v unzip >/dev/null || { echo "ERROR: unzip is required (ZIP64 support needed)"; exit 1; }
mkdir -p "$STAGING"

# Preflight: fail fast if any URL is wrong before pulling tens of GB.
log "Preflighting URLs..."
for u in "$CLI_URL" "${DATA_BASE}/${DATA_ZIP}" "${DATA_BASE}/${PHENO_ZIP}"; do
  if ! curl -sILf "$u" >/dev/null; then
    echo "ERROR: URL not reachable: $u"
    echo "  Check EXOMISER_VERSION / EXOMISER_DATA_VERSION / EXOMISER_ASSEMBLY."
    exit 1
  fi
  echo "  ok: $u"
done

avail=$(df -Ph "$EXOMISER_DIR" 2>/dev/null | awk 'NR==2{print $4}' || echo '?')
log "Free space at ${EXOMISER_DIR}: ${avail} (need ~80 GB+ for download + extraction)."

dl() {  # url dest — resumable, retrying
  log "Downloading $(basename "$2") ..."
  curl -fL --retry 5 --retry-delay 5 --continue-at - -o "$2" "$1"
}

dl "$CLI_URL" "${STAGING}/${CLI_ZIP}"
dl "${DATA_BASE}/${DATA_ZIP}" "${STAGING}/${DATA_ZIP}"
dl "${DATA_BASE}/${PHENO_ZIP}" "${STAGING}/${PHENO_ZIP}"
# TODO: verify SHA256 once the project pins expected checksums (upstream does not publish them).

log "Extracting CLI..."
unzip -oq "${STAGING}/${CLI_ZIP}" -d "$EXOMISER_DIR"
log "Extracting data into ${INSTALL_DIR}/data ..."
mkdir -p "${INSTALL_DIR}/data"
unzip -oq "${STAGING}/${DATA_ZIP}" -d "${INSTALL_DIR}/data"
unzip -oq "${STAGING}/${PHENO_ZIP}" -d "${INSTALL_DIR}/data"

log "Removing staged ZIPs to reclaim space..."
rm -f "${STAGING}/${CLI_ZIP}" "${STAGING}/${DATA_ZIP}" "${STAGING}/${PHENO_ZIP}"
rmdir "$STAGING" 2>/dev/null || true

# Configure application.properties for the assembly we actually downloaded. The shipped
# v15 defaults enable hg19 and comment out hg38; on an hg38-only install Exomiser would
# fail at startup trying to load the (absent) hg19 data. Enable the downloaded assembly +
# phenotype version and disable the other assembly. (Portable sed: temp file + mv.)
PROPS="${INSTALL_DIR}/application.properties"
if [ -f "$PROPS" ]; then
  log "Configuring application.properties for ${EXOMISER_ASSEMBLY} (data ${EXOMISER_DATA_VERSION})..."
  if [ "$EXOMISER_ASSEMBLY" = "hg38" ]; then OTHER="hg19"; else OTHER="hg38"; fi
  sed \
    -e "s|^#*\(exomiser\.${EXOMISER_ASSEMBLY}\.data-version\)=.*|\1=${EXOMISER_DATA_VERSION}|" \
    -e "s|^\(exomiser\.${OTHER}\.data-version\)=|#\1=|" \
    -e "s|^#*\(exomiser\.phenotype\.data-version\)=.*|\1=${EXOMISER_DATA_VERSION}|" \
    "$PROPS" > "${PROPS}.tmp" && mv "${PROPS}.tmp" "$PROPS"
fi

cat > "${INSTALL_DIR}/openscientist-exomiser-manifest.txt" <<EOF
exomiser_cli_version=${EXOMISER_VERSION}
exomiser_data_version=${EXOMISER_DATA_VERSION}
assembly=${EXOMISER_ASSEMBLY}
cli_url=${CLI_URL}
data_url=${DATA_BASE}/${DATA_ZIP}
phenotype_url=${DATA_BASE}/${PHENO_ZIP}
installed_at=$(date -u +%FT%TZ)
EOF

log "Done."
echo "application.properties configured: ${EXOMISER_ASSEMBLY}/phenotype data-version=${EXOMISER_DATA_VERSION}."
echo "Next, in your .env set:"
echo "     EXOMISER_HOST_PATH=$(cd "$INSTALL_DIR" && pwd)"
echo "(the run_exomiser tool pins exomiser.data-directory at runtime, so no manual edit needed)"
echo "(genome preset additionally needs REMM data — see notes/exomiser-integration-plan.md)"
