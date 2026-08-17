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

# How the remote path must be written depends on the local rsync.
#
# Classic rsync hands the remote path to the remote shell, so a path with a
# space in it (e.g. "Mobile Documents") gets word-split and the files land in a
# truncated directory. Quoting the path fixes that.
#
# rsync 3.x has --protect-args (-s), which sends the path over the protocol
# instead of through a shell — and enables it by default in recent versions.
# There the quotes are never shell syntax, so they end up as literal characters
# in the filename and the transfer fails.
#
# So: use --protect-args where it exists, and fall back to quoting where it does
# not (macOS ships openrsync, which has neither the option nor the shell issue
# handled for us).
# The two branches are spelled out rather than built up in an array: macOS
# ships bash 3.2, where expanding an empty array under `set -u` aborts.
#
# --delete removes files from dest that no longer exist at source
if rsync --protect-args --version >/dev/null 2>&1; then
  rsync -avz --progress --delete \
    --exclude=".backer-info" \
    --protect-args \
    "${SOURCE}/" \
    "${FRAMBOISE}:${DEST_PATH}/"
else
  rsync -avz --progress --delete \
    --exclude=".backer-info" \
    "${SOURCE}/" \
    "${FRAMBOISE}:'${DEST_PATH}/'"
fi

# Write a manifest marker so the dashboard knows this is a tracked backup root
TIMESTAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
INFO_JSON=$(printf '{"source_host":"%s","source_path":"%s","source_user":"%s","last_push":"%s"}' \
  "$SRC_HOST" "$SOURCE" "$SRC_USER" "$TIMESTAMP")

ssh "$FRAMBOISE" "printf '%s\n' '${INFO_JSON}' > '${DEST_PATH}/.backer-info'"

echo ""
echo "Done. Dashboard: http://${FRAMBOISE}:8765"
