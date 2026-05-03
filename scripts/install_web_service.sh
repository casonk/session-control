#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
SERVICE_FILE="$SERVICE_DIR/session-control-web.service"
ENV_FILE="$ROOT/config/session-control.env.local"

mkdir -p "$SERVICE_DIR"

cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=session-control private web UI
After=network.target

[Service]
Type=simple
WorkingDirectory=$ROOT
Environment=SESSION_CONTROL_ENV_FILE=$ENV_FILE
ExecStart=$ROOT/.venv/bin/session-control web
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
SERVICE

systemctl --user daemon-reload
systemctl --user enable --now session-control-web.service
systemctl --user status session-control-web.service --no-pager
