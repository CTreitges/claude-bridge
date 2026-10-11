# claude-bridge

Startet per Sprache neue [Claude-Code](https://code.claude.com)-Sessions auf deinem eigenen
Server. Du tippst in der Android-App [WhisperLoom](https://github.com/CTreitges/whisperloom-android)
auf das Widget „Claude Code", sprichst den Auftrag und tippst erneut (oder lässt die Sprechpause
die Aufnahme beenden). Die Bridge startet sofort eine
neue Session, die direkt mit dem Auftrag beginnt — und dir als Erstes eine Push-Benachrichtigung
schickt. Weiter geht es in der Claude-App über Remote Control oder am Terminal.

> **In English:** a small self-hosted FastAPI service. WhisperLoom's home screen widget records
> your voice, transcribes it and POSTs the text here; this service starts a fresh interactive
> Claude Code session in tmux with Remote Control enabled, so it shows up in the Claude app and
> pushes a "session started" notification. One endpoint, one bearer token. Model, effort and
> ultracode come from the widget's profile. Code and comments are in German.

Vorbild ist [hermes-bridge](https://github.com/CTreitges/hermes-bridge): derselbe Vertrag zur
App, dieselbe Art der Einrichtung.

## Was du brauchst

- Einen Rechner, der dauerhaft läuft und aus dem Internet erreichbar ist (VPS, Heimserver mit
  Portfreigabe oder Tailscale), mit **Python 3.11+** samt `python3-venv`, **tmux** und **curl**.
- **Claude Code**, angemeldet mit einem claude.ai-Konto (Remote Control braucht das), unter
  `~/.local/bin/claude`. Einmal von Hand im Arbeitsverzeichnis (Standard: Home) gestartet, damit
  der Vertrauens-Dialog erledigt ist.
- In Claude Code `/config` → **Push when Claude decides** an (Einstellung `agentPushNotifEnabled`;
  die Bridge setzt sie für ihre Sessions ohnehin) und die Claude-App auf dem Telefon.
- TLS davor. Ohne HTTPS geht das Token im Klartext über die Leitung.
- WhisperLoom mit dem Widget „Claude Code".

Der Dienst bindet ausschließlich auf `127.0.0.1` — nach außen führt einzig der Reverse-Proxy.

> **Das Token ist ein Schlüssel zu deinem Server.** Wer es kennt, lässt Claude mit deinen Rechten
> arbeiten — mit den Berechtigungen, die du Claude Code gegeben hast (bei `bypassPermissions`
> also ohne Rückfrage). Lang und zufällig, nur in `.env` und in der App, nie im Chat.

## Ablauf eines Auftrags

1. `POST /v1/task` mit Bearer-Token.
2. Die Bridge legt den Auftrag als Datei ab (0600) und startet eine tmux-Sitzung
   `cb-<session-id>`. Darin liest `app/launch.py` die Datei, löscht sie und ersetzt sich durch
   ```
   claude --model opus[1m] --effort xhigh --settings '{"ultracode": false, …}' \
          --session-id <id> --name "Sprachauftrag: …" --remote-control "Sprachauftrag: …" \
          --append-system-prompt "<Kontext + Start-Push>" -- "<Transkript>"
   ```
3. Nach zwei Sekunden sieht die Bridge nach, ob die Session noch läuft, und antwortet **202**.
4. Claude schickt als Erstes `PushNotification` („Claude Bridge: Session gestartet – …") und
   arbeitet dann den Auftrag ab. Die Session bleibt offen: Rückfragen, Weiterreden, Abbrechen —
   in der Claude-App oder mit `tmux attach -t cb-<anfang der id>`.

<details>
<summary>Warum der Push eine Umgebungsvariable braucht</summary>

`PushNotification` unterdrückt den Push, solange jemand am Terminal sitzt. Den Startprompt,
den die Bridge übergibt, wertet Claude Code als genau so eine Eingabe — das Werkzeug meldet dann
„Not sent — this terminal is active". Die Bridge setzt deshalb für ihre Sessions
`CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK=1`; dann kommt „Mobile push requested".
Gemessen mit Claude Code 2.1.296.
</details>

<details>
<summary>Warum das Transkript nicht auf der tmux-Kommandozeile steht</summary>

tmux liest ein Argument, das auf `;` endet, als Ende des Befehls; was danach kommt, wären neue
tmux-Befehle. Ein Transkript ist Nutzereingabe. Deshalb stehen auf der tmux-Zeile nur Werte,
die die Bridge selbst erzeugt, und der Auftrag geht über eine Datei. Ein Test hält das fest.
</details>

## Schnittstelle

```
POST /v1/task
Authorization: Bearer <token>
Content-Type: application/json

{"transcript": "…", "request_id": "<uuid>", "recorded_at": "<ISO-8601>",
 "duration_ms": 12345, "source": "widget",
 "options": {"model": "opus", "effort": "xhigh", "ultracode": false}}
```

`options` ist optional, ebenso jedes Feld darin; was fehlt, kommt aus `.env`.

| Feld | Werte | Standard |
|---|---|---|
| `model` | `opus` (→ `opus[1m]`), `fable`, `sonnet` (→ `sonnet[1m]`), `haiku` | `opus` |
| `effort` | `low`, `medium`, `high`, `xhigh`, `max` | `xhigh` |
| `ultracode` | `true` / `false` | `false` |

| Code | Wann |
|---|---|
| 202 | Session läuft (`session`, `title`, `model`, `effort`, `ultracode`; `duplicate: true`, wenn die `request_id` schon da war) |
| 400 | Transkript leer |
| 401 | Token fehlt oder stimmt nicht, oder der Dienst hat keins (≥ 32 Zeichen) |
| 422 | Transkript länger als 20 000 Zeichen, unbekanntes Modell oder Effort |
| 429 | mehr als 6 Aufträge pro Minute · schon 5 Sessions offen · 10 falsche Tokens in 10 Minuten von derselben Adresse |
| 503 | Session ließ sich nicht starten — die `request_id` wird wieder freigegeben |

`GET /health` verrät nichts Geheimes: `{"status": "ok", "configured": true}`. Ohne Token in
`.env` (oder mit einem kürzer als 32 Zeichen) meldet er `"configured": false` und beantwortet
jeden Auftrag mit 401.

Die `request_id` bleibt über alle Wiederholungen der App gleich; dieselbe Id startet nur eine
Session (eine Stunde lang gemerkt). Ein leeres Transkript beantwortet der Dienst mit 400,
**nachdem** er das Token geprüft hat — darauf baut „Verbindung prüfen" in der App.

## Einrichten

```bash
git clone https://github.com/CTreitges/claude-bridge.git ~/claude-bridge
~/claude-bridge/install.sh
```

`install.sh` legt die venv an, erzeugt eine `.env` mit neuem Token (chmod 600, das Token wird
nicht ausgegeben), installiert die systemd-User-Unit auf Port 8097 (`PORT=… ./install.sh` für
einen anderen) und startet sie. Es schaltet Linger ein, damit der Dienst ohne Anmeldung läuft,
und bricht mit dem Journal ab, wenn der Dienst nicht antwortet. Mehrfach ausführbar — eine
vorhandene `.env` bleibt, wie sie ist. Installiert wird aus `requirements.lock` (exakte Versionen).

Das Token für die App zeigst du **im eigenen Terminal** an (nicht in einer Claude-Session):

```bash
grep ^BRIDGE_TOKEN= ~/claude-bridge/.env | cut -d= -f2-
```

### TLS davor

Caddy (kümmert sich selbst um das Zertifikat):

```caddyfile
claude-bridge.example.de {
	encode zstd gzip
	header X-Robots-Tag "noindex, nofollow, noarchive"
	request_body {
		max_size 256KB
	}
	reverse_proxy 127.0.0.1:8097
}
```

`max_size` begrenzt, was ein Fremder ohne Token schicken kann: FastAPI liest den Body, bevor es
das Token prüft. 256 KB fassen auch 20 000 Zeichen Emoji.

Die Sperre nach falschen Tokens gilt je Absender und liest dafür den letzten Eintrag von
`X-Forwarded-For` — den setzt Caddy selbst. Ein anderer Proxy muss das genauso tun.

### In der App eintragen

WhisperLoom → Einstellungen → **Erweitert** → **Pro Widgets** einschalten → **Pro Widgets
verwalten** → **Neues Pro Widget** → **Claude-Code-Widget**: Server-Adresse
(`https://claude-bridge.example.de`, ohne Pfad), Token, **Verbindung prüfen**. Modell, Effort und
Ultracode stellst du im selben Profil ein. Mehrere Profile = mehrere Widgets, etwa „Claude"
(Opus, Extra hoch) und „Claude Ultracode" (Opus, Max, Ultracode an).

Dann auf dem Startbildschirm lange drücken → Widgets → WhisperLoom → **Pro Widget** hinziehen und
das Claude-Profil wählen. Bedienung: tippen, sprechen, tippen — ein Startbildschirm-Widget sieht
kein Loslassen des Fingers.

Das Widget gibt es ab dem nächsten F-Droid-Update von WhisperLoom; bis dahin im Test-APK der
GitHub-Releases (`v3.9.0-claude.*`).

## Konfiguration

Alles in `.env` (siehe `.env.example`):

| Schlüssel | Pflicht | Bedeutung |
|---|---|---|
| `BRIDGE_TOKEN` | ja | Was die App im `Authorization`-Header mitschickt. Mindestens 32 Zeichen. |
| `CLAUDE_DEFAULT_MODEL` / `_EFFORT` / `_ULTRACODE` | nein | Standard, wenn das Widget nichts schickt. |
| `CLAUDE_WORKDIR` | nein | Arbeitsverzeichnis der Sessions (Standard: Home — nie das Verzeichnis der Bridge). |
| `CLAUDE_MAX_SESSIONS` | nein | Höchstens so viele Bridge-Sessions gleichzeitig (Standard 5). |
| `CLAUDE_IDLE_HOURS` | nein | Sessions, die so lange nichts geschrieben haben, schließt die Bridge vor dem nächsten Start (Standard 24). |
| `CLAUDE_BIN` | nein | Pfad zu `claude` (Standard `~/.local/bin/claude`). |

Nach jeder Änderung: `systemctl --user restart claude-bridge`. Laufende Sessions überleben den
Neustart (`KillMode=process`).

## Sessions verwalten

```bash
tmux ls                          # Bridge-Sessions heißen cb-<session-id>
tmux attach -t cb-1a2b           # dazukommen (Anfang der Id genügt), lösen mit Strg-b d
tmux kill-session -t cb-1a2b     # beenden
claude --resume <session-id>     # eine geschlossene Session wieder aufnehmen
```

Geschlossen wird nur, was `cb-<uuid>` heißt — deine eigenen tmux-Sitzungen fasst die Bridge nie
an, und eine Session, die gerade arbeitet, auch nicht. Der Name bleibt auch nach `/clear` in
der Session gleich; die Bridge folgt dann der neuen Session-Id.

Bridge-Sessions tragen `CLAUDE_BRIDGE_SESSION=1` in ihrer Umgebung — praktisch für eigene
Skripte, die Claude-Prozesse zählen und die Bridge-Sessions dabei auslassen wollen.

**Datenschutz am Server:** Das Transkript steht während der Session in der Kommandozeile des
claude-Prozesses. Ist `/proc` ohne `hidepid` gemountet (Ubuntu-Standard), können andere lokale
Benutzer es mit `ps` lesen. Auf einem Ein-Personen-Server ist das meist egal.

## Wenn etwas nicht klappt

```bash
systemctl --user status claude-bridge
journalctl --user -u claude-bridge -n 50 --no-pager
```

```
INFO claude-bridge: Auftrag abc-123 → cb-5f0c… (opus, xhigh, Ultracode aus, 87 Zeichen)
```

- **„Verbindung prüfen" meldet 401** — Token in `.env` und App stimmen nicht überein (oder es
  ist kürzer als 32 Zeichen). Nach jeder Änderung an `.env` den Dienst neu starten.
- **503 „hat sich sofort wieder beendet"** — steht dahinter ein Grund (etwa
  `FileNotFoundError`), kam `claude` gar nicht erst in Gang: `CLAUDE_BIN` und `CLAUDE_WORKDIR`
  prüfen. Ohne Grund ist Claude selbst sofort ausgestiegen; dann den Aufruf so nachstellen, wie
  die Bridge ihn macht:
  `cd ${CLAUDE_WORKDIR:-~} && CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK=1 ~/.local/bin/claude --model 'opus[1m]' --effort xhigh --remote-control Test -- "Hallo"`.
  Meist fehlt die Anmeldung oder das Arbeitsverzeichnis ist nicht vertraut.
- **Session startet, aber kein Push** — in der Session nachsehen, was `PushNotification`
  meldete: „Remote Control inactive" heißt, Remote Control kam nicht hoch; „disabled in /config"
  heißt, `agentPushNotifEnabled` ist aus.
- **429 „Es laufen schon 5 Claude-Sessions"** — alte Sessions beenden (siehe oben) oder
  `CLAUDE_MAX_SESSIONS` erhöhen.

## Tests

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

Es startet nie eine echte Claude-Session. Die Integrationstests nehmen ein echtes tmux mit
eigenem Server und ein falsches `claude`, das nur seine Argumente aufschreibt.

## Lizenz

MIT.
