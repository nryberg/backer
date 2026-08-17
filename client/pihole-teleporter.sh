#!/usr/bin/env bash
# pihole-teleporter.sh — export Pi-hole's configuration and back it up
#
# Usage:
#   pihole-teleporter.sh [framboise-host]
#
# Runs Pi-hole's built-in Teleporter to produce a small, restorable archive
# (pihole.toml, /etc/hosts, DHCP leases, and your adlists/groups/clients),
# prunes older archives, then hands the directory to push.sh.
#
# Deliberately NOT a backup of /var/log/pihole: those logs are a ~6-day
# rolling spool that nothing can restore from, and they contain every DNS
# query on the network. The Teleporter archive is the part worth keeping.
#
# Note: Teleporter does not include the long-term query history in
# pihole-FTL.db. That database is written continuously, so copying it safely
# needs `sqlite3 ... ".backup"` rather than a plain file copy.
#
# Environment:
#   PIHOLE_EXPORT_DIR   where archives are kept   default: $HOME/pihole-teleporter
#   PIHOLE_EXPORT_KEEP  how many to retain        default: 14
#   PUSH_SCRIPT         path to push.sh           default: alongside this script

set -euo pipefail

FRAMBOISE="${1:-framboise}"
EXPORT_DIR="${PIHOLE_EXPORT_DIR:-${HOME}/pihole-teleporter}"
KEEP="${PIHOLE_EXPORT_KEEP:-14}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Find the push script — installed next to this one, under either name
PUSH="${PUSH_SCRIPT:-}"
if [[ -z "$PUSH" ]]; then
  for candidate in "${SCRIPT_DIR}/push.sh" "${SCRIPT_DIR}/push-to-${FRAMBOISE}"; do
    if [[ -x "$candidate" ]]; then
      PUSH="$candidate"
      break
    fi
  done
fi
if [[ -z "$PUSH" || ! -x "$PUSH" ]]; then
  echo "Cannot find push script next to $SCRIPT_DIR — set PUSH_SCRIPT=/path/to/push.sh" >&2
  exit 1
fi

if ! command -v pihole-FTL >/dev/null 2>&1; then
  echo "pihole-FTL not found — is this a Pi-hole host? (cron uses a minimal PATH)" >&2
  exit 1
fi

mkdir -p "$EXPORT_DIR"
cd "$EXPORT_DIR"   # pihole-FTL --teleporter writes into the current directory

ARCHIVE=$(pihole-FTL --teleporter | tail -1)
if [[ ! -s "$ARCHIVE" ]]; then
  echo "Teleporter produced no archive" >&2
  exit 1
fi
echo "Exported ${EXPORT_DIR}/${ARCHIVE} ($(du -h "$ARCHIVE" | cut -f1))"

# Prune the oldest, keeping the newest $KEEP. The timestamp in the filename
# is YYYY-MM-DD_HH-MM-SS, so a lexicographic sort is chronological.
mapfile -t archives < <(ls -1 pi-hole_*_teleporter_*.zip 2>/dev/null | sort)
if (( ${#archives[@]} > KEEP )); then
  for old in "${archives[@]:0:${#archives[@]} - KEEP}"; do
    rm -f -- "$old"
    echo "Pruned $old"
  done
fi

exec "$PUSH" "$EXPORT_DIR" "$FRAMBOISE"
