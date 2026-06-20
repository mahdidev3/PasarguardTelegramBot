#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="pasarguardtelegrambot"
PROJECT_DIR="/home/PasarguardTelegramBot"
SERVICE_FILE="${SERVICE_NAME}.service"
SYSTEMD_DIR="/etc/systemd/system"

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root: sudo bash install_service.sh"
  exit 1
fi

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "Project directory not found: $PROJECT_DIR"
  exit 1
fi

if [[ ! -x "$PROJECT_DIR/.venv/bin/python" ]]; then
  echo "Python venv not found or not executable: $PROJECT_DIR/.venv/bin/python"
  echo "Create it first and install requirements."
  exit 1
fi

if [[ ! -f "$PROJECT_DIR/main.py" ]]; then
  echo "main.py not found: $PROJECT_DIR/main.py"
  exit 1
fi

cp "$(dirname "$0")/$SERVICE_FILE" "$SYSTEMD_DIR/$SERVICE_FILE"
chmod 644 "$SYSTEMD_DIR/$SERVICE_FILE"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
systemctl --no-pager --full status "$SERVICE_NAME" || true

echo
 echo "Installed and started: $SERVICE_NAME"
echo "Logs: journalctl -u $SERVICE_NAME -f"











