#!/usr/bin/env bash
# Richtet claude-bridge als systemd-User-Dienst ein. Mehrfach ausfuehrbar: eine vorhandene
# .env bleibt unangetastet, das Token erscheint weder in der Ausgabe noch in einer
# Kommandozeile.
#
#   ./install.sh                                  # Port 8097
#   PORT=8123 ./install.sh                        # anderer Port
#   CLAUDE_BIN=/usr/local/bin/claude ./install.sh # claude liegt woanders
set -euo pipefail

cd "$(dirname "$0")"
DIR="$(pwd)"
PORT="${PORT:-8097}"
UNIT="$HOME/.config/systemd/user/claude-bridge.service"

fehlt() { echo "FEHLT: $1" >&2; exit 1; }

command -v python3 >/dev/null || fehlt "python3 (3.11 oder neuer)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' || fehlt "python3 ab 3.11"
command -v tmux >/dev/null || fehlt "tmux"
command -v curl >/dev/null || fehlt "curl"
CLAUDE_BIN="${CLAUDE_BIN:-$HOME/.local/bin/claude}"
[ -x "$CLAUDE_BIN" ] || fehlt "claude unter $CLAUDE_BIN (Claude Code installieren und einmal anmelden)"
case "$PORT" in ''|*[!0-9]*) fehlt "PORT als Zahl" ;; esac

echo "== venv"
# Eine halbe venv (python3-venv fehlte beim ersten Lauf) wird neu angelegt.
if [ ! -x .venv/bin/pip ]; then
  rm -rf .venv
  python3 -m venv .venv || fehlt "python3-venv (sudo apt install python3-venv)"
fi
.venv/bin/pip install -q -r requirements.lock

echo "== .env"
if [ -f .env ]; then
  echo "   vorhanden, bleibt wie sie ist"
  if [ "$CLAUDE_BIN" != "$HOME/.local/bin/claude" ] && ! grep -q "^CLAUDE_BIN=$CLAUDE_BIN\$" .env; then
    echo "   WARNUNG: CLAUDE_BIN=$CLAUDE_BIN steht nicht in .env — dort eintragen" >&2
  fi
else
  # Token, Vorlage und Ziel bleiben in Python: das Token taucht in keiner Kommandozeile auf.
  CLAUDE_BIN="$CLAUDE_BIN" python3 - <<'PY'
import os
import secrets

lines = []
for line in open(".env.example", encoding="utf-8").read().splitlines():
    if line.strip().startswith("BRIDGE_TOKEN="):
        line = "BRIDGE_TOKEN=" + secrets.token_urlsafe(32)
    lines.append(line)
claude_bin = os.environ["CLAUDE_BIN"]
if claude_bin != os.path.expanduser("~/.local/bin/claude"):
    lines.append("CLAUDE_BIN=" + claude_bin)
fd = os.open(".env", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
PY
  echo "   angelegt mit neuem Token (chmod 600)"
fi
chmod 600 .env

echo "== systemd-User-Unit"
mkdir -p "$(dirname "$UNIT")"
# Ersetzen in Python, nicht mit sed: Pfade mit & oder | und ein Home wie /home/christian
# (das mit /home/chris beginnt) blieben sonst nicht heil.
python3 - "$DIR" "$HOME" "$PORT" "$UNIT" <<'PY'
import sys

repo, home, port, unit = sys.argv[1:]
text = open("deploy/claude-bridge.service", encoding="utf-8").read()
text = text.replace("@DIR@", repo).replace("@HOME@", home).replace("@PORT@", port)
open(unit, "w", encoding="utf-8").write(text)
PY
systemctl --user daemon-reload
systemctl --user enable claude-bridge
systemctl --user restart claude-bridge

echo "== Health"
ok=0
for _ in $(seq 1 20); do
  if curl -fs "http://127.0.0.1:$PORT/health"; then echo; ok=1; break; fi
  sleep 0.5
done
if [ "$ok" != 1 ]; then
  echo "Der Dienst antwortet nicht auf Port $PORT:" >&2
  systemctl --user status claude-bridge --no-pager >&2 || true
  journalctl --user -u claude-bridge -n 20 --no-pager >&2 || true
  exit 1
fi

echo "== Linger"
# Ohne Linger beendet systemd den Dienst (und mit ihm die Sessions) beim Abmelden.
if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" = yes ]; then
  echo "   an"
elif loginctl enable-linger "$USER" 2>/dev/null; then
  echo "   eingeschaltet"
else
  echo "   WARNUNG: konnte Linger nicht einschalten — 'loginctl enable-linger $USER' (ggf. mit sudo)" >&2
fi

cat <<TEXT

Fertig. Noch zu tun:
  1. TLS davor (Caddy/nginx, siehe README) auf 127.0.0.1:$PORT.
  2. Token in der App eintragen — anzeigen (nur im eigenen Terminal!):
       grep ^BRIDGE_TOKEN= $DIR/.env | cut -d= -f2-
TEXT
