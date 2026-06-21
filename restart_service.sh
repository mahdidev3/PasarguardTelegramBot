#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="pasarguardtelegrambot"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo bash restart_service.sh"
  exit 1
fi

systemctl daemon-reload
systemctl restart "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME" || true
