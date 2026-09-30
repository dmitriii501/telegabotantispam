#!/usr/bin/env bash
# Every 2 minutes: is the bot alive? Sends a Telegram message to ALERT_CHAT_ID when it goes down and when it is back.
# Checks: the systemd service, the /health endpoint (event loop alive), free disk space.
set -uo pipefail

APP_DIR=${APP_DIR:-/opt/defenceai}
STATE=${STATE:-/var/lib/defenceai-health.state}
set -a; . "$APP_DIR/.env"; set +a

problem=""
systemctl is-active --quiet defenceai || problem="служба defenceai не запущена"
if [ -z "$problem" ] && [ -n "${WEBAPP_URL:-}" ]; then
  curl -fsS --max-time 10 "http://${WEBAPP_HOST:-127.0.0.1}:${WEBAPP_PORT:-8080}/health" >/dev/null 2>&1 \
    || problem="бот не отвечает (проверка /health не прошла)"
fi
if [ -z "$problem" ]; then
  used=$(df --output=pcent "$APP_DIR" | tail -1 | tr -dc '0-9')
  [ "${used:-0}" -ge 90 ] && problem="диск заполнен на ${used}%"
fi

send() {
  [ -n "${ALERT_CHAT_ID:-}" ] || return 0
  curl -sS --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${ALERT_CHAT_ID}" --data-urlencode "text=$1" >/dev/null || true
}

previous=$(cat "$STATE" 2>/dev/null || true)
if [ -n "$problem" ]; then
  echo "$problem"
  if [ "$previous" != "down" ]; then send "🔴 DefenceAi: $problem"; echo down > "$STATE"; fi
  exit 1
fi
if [ "$previous" = "down" ]; then send "🟢 DefenceAi снова работает"; fi
echo ok > "$STATE"
