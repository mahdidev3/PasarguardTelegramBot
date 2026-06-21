#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="pasarguardtelegrambot"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo bash uninstall_service.sh"
  exit 1
fi

systemctl stop "$SERVICE_NAME" || true
systemctl disable "$SERVICE_NAME" || true
rm -f "$SERVICE_FILE"
systemctl daemon-reload
systemctl reset-failed "$SERVICE_NAME" || true

echo "Removed: $SERVICE_NAME"
