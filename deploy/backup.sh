#!/usr/bin/env bash
# Daily consistent copy of the bot database (comments, rules, settings), kept for 14 days.
# Restore: systemctl stop defenceai; gunzip -c /var/backups/defenceai/bot-YYYY-MM-DD.db.gz > /opt/defenceai/bot.db;
#          chown defenceai:defenceai /opt/defenceai/bot.db; systemctl start defenceai
set -euo pipefail

APP_DIR=${APP_DIR:-/opt/defenceai}
DEST=${DEST:-/var/backups/defenceai}
KEEP_DAYS=${KEEP_DAYS:-14}

install -d -m 700 "$DEST"
out="$DEST/bot-$(date +%F).db"
rm -f "$out" "$out.gz"

# sqlite3's online backup API is safe while the bot is writing.
"$APP_DIR/.venv/bin/python" - "$APP_DIR/bot.db" "$out" <<'PY'
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
gzip -f "$out"
chmod 600 "$out.gz"
find "$DEST" -name 'bot-*.db.gz' -mtime +"$KEEP_DAYS" -delete
echo "Backup saved: $out.gz"
