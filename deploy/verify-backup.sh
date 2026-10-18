#!/usr/bin/env bash
# Weekly: restore the newest backup into a temporary folder and check it. Tells you in Telegram when it is not fine
# (and once a month that it is). Needs ALERT_CHAT_ID in .env, like the health check.
set -uo pipefail

APP_DIR=${APP_DIR:-/opt/defenceai}
DEST=${DEST:-/var/backups/defenceai}
set -a; . "$APP_DIR/.env"; set +a

send() {
  [ -n "${ALERT_CHAT_ID:-}" ] || return 0
  curl -sS --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${ALERT_CHAT_ID}" --data-urlencode "text=$1" >/dev/null || true
}

cd "$APP_DIR" || exit 1
output=$("$APP_DIR/.venv/bin/python" -m scripts.verify_backup --dest "$DEST" --live "${DB_PATH:-$APP_DIR/bot.db}" 2>&1)
status=$?
echo "$output"
if [ $status -ne 0 ]; then
  send "🔴 DefenceAi: резервная копия не проходит проверку. $output"
elif [ "$(date +%d)" -le 7 ]; then
  send "🟢 DefenceAi: резервная копия восстанавливается. $output"
fi
exit $status
