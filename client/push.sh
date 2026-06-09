#!/usr/bin/env bash
# push.sh — sync a local directory to the Backer store on framboise
#
# Usage:
#   push.sh <source-path> [framboise-host]
#
# Examples:
#   push.sh /home/nick/documents
#   push.sh /var/www/html myserver
#   push.sh ~/projects framboise.local
#
# The data lands at:
#   <framboise>:/mnt/backup/<this-hostname>/<source-path>/
#
# A .backer-info marker is written at the destination so the dashboard
# can show the original source path and last-push time.

set -euo pipefail

SOURCE="${1:?Usage: push.sh <source-path> [framboise-host]}"
FRAMBOISE="${2:-framboise}"
BACKUP_ROOT="/mnt/backup"

SRC_HOST=$(hostname -s)
SRC_USER=$(whoami)

# Resolve to an absolute path (works on macOS and Linux)
SOURCE=$(cd "$SOURCE" && pwd)

# Strip the leading slash so we can nest it under BACKUP_ROOT/<hostname>/
DEST_SUBPATH="${SOURCE#/}"
DEST_PATH="${BACKUP_ROOT}/${SRC_HOST}/${DEST_SUBPATH}"

echo "Source : ${SRC_USER}@${SRC_HOST}:${SOURCE}/"
echo "Dest   : ${FRAMBOISE}:${DEST_PATH}/"
echo ""

# Create destination directory
ssh "$FRAMBOISE" "mkdir -p '${DEST_PATH}'"

# Sync — delete removes files from dest that no longer exist at source
rsync -avz --progress --delete \
  --exclude=".backer-info" \
  "${SOURCE}/" \
  "${FRAMBOISE}:${DEST_PATH}/"

# Write a manifest marker so the dashboard knows this is a tracked backup root
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
INFO_JSON=$(printf '{"source_host":"%s","source_path":"%s","source_user":"%s","last_push":"%s"}' \
  "$SRC_HOST" "$SOURCE" "$SRC_USER" "$TIMESTAMP")

ssh "$FRAMBOISE" "printf '%s\n' '${INFO_JSON}' > '${DEST_PATH}/.backer-info'"

echo ""
echo "Done. Dashboard: http://${FRAMBOISE}:8765"
