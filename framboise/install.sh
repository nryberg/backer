#!/usr/bin/env bash
# install.sh — set up the Backer dashboard on framboise
#
# Run as root on framboise:
#   sudo bash install.sh
#
# Optional: pass the USB device to mount it automatically:
#   sudo bash install.sh /dev/sda1
#   sudo bash install.sh /dev/sda1 /mnt/backup
#
# After installation the dashboard is at http://framboise:8765

set -euo pipefail

DEVICE="${1:-}"
MOUNT="${2:-/mnt/backup}"
INSTALL_DIR="/opt/backer"
SERVICE="backup-dashboard"
HOSTNAME_VAL=$(hostname -s)

if [[ $EUID -ne 0 ]]; then
  echo "Run as root: sudo bash install.sh" >&2
  exit 1
fi

# ---- Determine the service user (pi or the first non-root user) ----
if id pi &>/dev/null; then
  SVC_USER=pi
else
  SVC_USER=$(awk -F: '$3 >= 1000 && $3 < 65534 {print $1; exit}' /etc/passwd)
fi
echo "Service will run as: ${SVC_USER}"

# ---- Mount point ----
mkdir -p "$MOUNT"

if [[ -n "$DEVICE" ]]; then
  if mountpoint -q "$MOUNT"; then
    echo "$MOUNT already mounted — skipping mount"
  else
    echo "Mounting $DEVICE → $MOUNT"
    mount "$DEVICE" "$MOUNT"

    if ! grep -q "$MOUNT" /etc/fstab; then
      UUID=$(blkid -s UUID -o value "$DEVICE")
      FSTYPE=$(blkid -s TYPE -o value "$DEVICE")
      printf 'UUID=%s\t%s\t%s\tdefaults,nofail\t0\t2\n' \
        "$UUID" "$MOUNT" "$FSTYPE" >> /etc/fstab
      echo "Added $MOUNT to /etc/fstab (UUID=$UUID, type=$FSTYPE)"
    fi
  fi
else
  echo "No device specified — skipping mount (you can mount $MOUNT manually or re-run with a device)"
fi

# ---- Install server ----
mkdir -p "$INSTALL_DIR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "${SCRIPT_DIR}/server.py" "${INSTALL_DIR}/server.py"
chmod +x "${INSTALL_DIR}/server.py"
echo "Installed server.py → $INSTALL_DIR"

# ---- Systemd service ----
SERVICE_FILE="/etc/systemd/system/${SERVICE}.service"
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=Backer Dashboard
Documentation=https://github.com/nryberg/backer
After=network.target local-fs.target

[Service]
Type=simple
User=${SVC_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=BACKUP_ROOT=${MOUNT}
Environment=PORT=8765
Environment=FRAMBOISE_HOST=${HOSTNAME_VAL}
ExecStart=/usr/bin/python3 ${INSTALL_DIR}/server.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"

sleep 1
if systemctl is-active --quiet "$SERVICE"; then
  LOCAL_IP=$(hostname -I | awk '{print $1}')
  echo ""
  echo "Backer dashboard is running."
  echo "  http://${LOCAL_IP}:8765"
  echo "  http://${HOSTNAME_VAL}:8765"
  echo ""
  echo "  Status:  sudo systemctl status $SERVICE"
  echo "  Logs:    sudo journalctl -u $SERVICE -f"
else
  echo "Service failed to start — check logs:" >&2
  journalctl -u "$SERVICE" --no-pager -n 20 >&2
  exit 1
fi
