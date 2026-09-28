#!/usr/bin/env bash
# One-shot installer for Ubuntu 22.04+: installs the bot as a systemd service.
# Usage (as root, from the repository root):  bash deploy/install.sh
set -euo pipefail

APP_DIR=/opt/defenceai
SERVICE=defenceai

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root." >&2
  exit 1
fi

apt-get update -q
apt-get install -y -q python3 python3-venv python3-pip rsync

id -u defenceai >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin defenceai

mkdir -p "$APP_DIR"
rsync -a --delete --exclude '.git' --exclude '.env' --exclude 'bot.db' --exclude '.venv' --exclude '__pycache__' ./ "$APP_DIR"/

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --upgrade pip
"$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"

if [ ! -f "$APP_DIR/.env" ]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  echo
  echo "Created $APP_DIR/.env. Fill in TELEGRAM_BOT_TOKEN and TYPESAFE_API_KEY:"
  echo "  nano $APP_DIR/.env"
  echo "then run:  systemctl restart $SERVICE"
fi
chown -R defenceai:defenceai "$APP_DIR"
chmod 600 "$APP_DIR/.env"

cp deploy/defenceai.service /etc/systemd/system/$SERVICE.service
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl restart "$SERVICE"
echo
echo "Installed. Status:  systemctl status $SERVICE    Logs:  journalctl -u $SERVICE -f"
