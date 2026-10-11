#!/usr/bin/env bash
# Richtet claude-bridge als systemd-User-Dienst ein. Mehrfach ausfuehrbar: eine vorhandene
# .env bleibt unangetastet, das Token wird nie ausgegeben.
#
#   ./install.sh            # Port 8097
#   PORT=8123 ./install.sh  # anderer Port
set -euo pipefail

cd "$(dirname "$0")"
DIR="$(pwd)"
PORT="${PORT:-8097}"

fehlt() { echo "FEHLT: $1" >&2; exit 1; }

command -v python3 >/dev/null || fehlt "python3 (3.11 oder neuer)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fehlt "python3 ab 3.11"
command -v tmux >/dev/null || fehlt "tmux"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
[ -x "$CLAUDE_BIN" ] || fehlt "claude unter $CLAUDE_BIN (Claude Code installieren und einmal anmelden)"

echo "== venv"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt

echo "== .env"
if [ -f .env ]; then
  echo "   vorhanden, bleibt wie sie ist"
else
  umask 077
  sed "s|^BRIDGE_TOKEN=.*|BRIDGE_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')|" \
    .env.example > .env
  echo "   angelegt mit neuem Token (chmod 600)"
fi
chmod 600 .env

echo "== systemd-User-Unit"
mkdir -p "$HOME/.config/systemd/user"
sed -e "s|/home/chris/claude-bridge|$DIR|g" -e "s|/home/chris|$HOME|g" -e "s|--port 8097|--port $PORT|" \
  deploy/claude-bridge.service > "$HOME/.config/systemd/user/claude-bridge.service"
systemctl --user daemon-reload
systemctl --user enable --now claude-bridge
systemctl --user restart claude-bridge

echo "== Health"
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:$PORT/health"; then echo; break; fi
  sleep 0.5
done

cat <<TEXT

Fertig. Noch zu tun:
  1. TLS davor (Caddy/nginx, siehe README) auf 127.0.0.1:$PORT.
  2. Token in der App eintragen — anzeigen (nur im eigenen Terminal!):
       grep ^BRIDGE_TOKEN= $DIR/.env | cut -d= -f2-
  3. Damit der Dienst ohne Anmeldung laeuft: loginctl enable-linger $USER
TEXT
