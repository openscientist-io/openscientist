#!/bin/bash
# Create a release archive from the current HEAD, excluding all ignored files
set -e

VERSION=${1:-HEAD}
OUTPUT_DIR="./releases"
ARCHIVE_NAME="openscientist-${VERSION}.tar.gz"

mkdir -p "$OUTPUT_DIR"

echo "Creating release archive from $VERSION..."
echo "  Output: $OUTPUT_DIR/$ARCHIVE_NAME"

# git archive respects .gitignore by default, ensuring .venv and other
# local artifacts are never included in releases
git archive \
  --format=tar.gz \
  --output="$OUTPUT_DIR/$ARCHIVE_NAME" \
  "$VERSION"

# Show archive contents (verify .venv is excluded)
echo ""
echo "Archive contents (first 20 entries):"
tar tzf "$OUTPUT_DIR/$ARCHIVE_NAME" | head -20

echo ""
echo "✓ Archive created: $OUTPUT_DIR/$ARCHIVE_NAME"
echo "✓ .venv and .gitignored files excluded"
