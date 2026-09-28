#!/usr/bin/env bash
# Puts the web panel behind HTTPS: installs Caddy, which gets and renews a free
# certificate for the domain by itself, and points the bot at the public URL.
# Usage (as root, after deploy/install.sh):  bash deploy/setup_https.sh defenceaibot.duckdns.org
set -euo pipefail

DOMAIN=${1:?"Usage: bash deploy/setup_https.sh your.domain.example"}
ENV_FILE=/opt/defenceai/.env
PORT=${WEBAPP_PORT:-8080}

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root." >&2
  exit 1
fi
if [ ! -f "$ENV_FILE" ]; then
  echo "Run deploy/install.sh first." >&2
  exit 1
fi

# The domain must already point at this server, otherwise no certificate can be issued.
server_ip=$(curl -4 -fsS -m 10 https://api.ipify.org || true)
domain_ip=$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)
if [ -n "$server_ip" ] && [ "$server_ip" != "$domain_ip" ]; then
  echo "WARNING: $DOMAIN resolves to '${domain_ip:-nothing}', but this server is $server_ip." >&2
  echo "Fix the DNS record first, or the certificate will not be issued." >&2
  exit 1
fi

if ! command -v caddy >/dev/null; then
  apt-get update -q
  apt-get install -y -q debian-keyring debian-archive-keyring apt-transport-https curl gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -q
  apt-get install -y -q caddy
fi

cat > /etc/caddy/Caddyfile <<CADDY
$DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:$PORT
}
CADDY

# Ports 80 and 443 must be open for the certificate check and for Telegram.
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp
  ufw allow 443/tcp
fi

systemctl enable caddy >/dev/null
systemctl restart caddy

if grep -q '^WEBAPP_URL=' "$ENV_FILE"; then
  sed -i "s|^WEBAPP_URL=.*|WEBAPP_URL=https://$DOMAIN|" "$ENV_FILE"
else
  echo "WEBAPP_URL=https://$DOMAIN" >> "$ENV_FILE"
fi
chown defenceai:defenceai "$ENV_FILE"
chmod 600 "$ENV_FILE"

systemctl restart defenceai
echo
echo "Done. Panel: https://$DOMAIN   (send /panel to the bot)"
echo "Caddy logs: journalctl -u caddy -f"
