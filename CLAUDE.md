# claude-bridge — Hinweise für Claude

FastAPI-Dienst (Port 8097, nur 127.0.0.1): WhisperLoom-Widget → `POST /v1/task` → neue
interaktive Claude-Code-Session in tmux (`cb-<session-id>`) mit Remote Control + Start-Push.

- Aufbau: `app/main.py` (HTTP, Token, Limits), `app/sessions.py` (tmux, argv, Aufräumen),
  `app/launch.py` (läuft in tmux, liest Auftragsdatei, `execve` claude), `app/config.py`.
- Tests: `.venv/bin/python -m pytest tests -q` — nie eine echte Claude-Session starten; die
  Integrationstests nutzen ein eigenes tmux (`TMUX_TMPDIR`) und ein falsches `claude`.
- Nie Nutzertext auf die tmux-Kommandozeile (tmux liest `…;` als Befehlsende) — nur über die
  Auftragsdatei (0600).
- Sessions des Nutzers (`claude`, `claude2`, …) nie anfassen: nur Präfix `cb-`, exakte Ziele `=name`.
- Token nie ausgeben, loggen oder committen; `.env` ist chmod 600 und in `.gitignore`.
- Push braucht `CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK=1` (sonst „terminal is active").
- `KillMode=process` in der Unit nicht entfernen: sonst beendet ein Bridge-Neustart alle Sessions.
