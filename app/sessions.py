"""Claude-Code-Sessions in tmux starten, zaehlen und aufraeumen.

Jeder Auftrag wird eine eigene, interaktive Session — mit Remote Control, damit man sie in
der Claude-App sieht und dort weiterreden kann. Interaktiv heisst: Claude braucht ein
Terminal. Das liefert tmux; die Session heisst `cb-<session-id>`, man kommt jederzeit mit
`tmux attach -t cb-<anfang der id>` dazu.

Das Transkript ist Nutzereingabe und geht NIE ueber die tmux-Kommandozeile: tmux liest ein
Argument, das auf `;` endet, als Ende des Befehls — dahinter stehende Woerter waeren neue
tmux-Befehle. Der Auftrag liegt deshalb in einer Datei (0600), die `launch.py` in der
Sitzung liest, sofort loescht und an `claude` weiterreicht. Auf der tmux-Zeile stehen nur
Werte, die die Bridge selbst erzeugt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger("claude-bridge")

LAUNCHER = Path(__file__).resolve().parent / "launch.py"

#: Schaltet die Anwesenheitspruefung des PushNotification-Werkzeugs ab. Ohne sie gilt der
#: frisch uebergebene Startprompt als Tippen am Terminal, und der Push an das Handy
#: unterbleibt mit "Not sent — this terminal is active" (gemessen 2026-10-11, 2.1.296).
PUSH_ENV = {"CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK": "1"}

#: Wird an den System-Prompt der Session gehaengt — unsichtbar in der App, anders als ein
#: Rahmen um das Transkript. Die erste Nachricht bleibt so genau das, was gesprochen wurde.
SYSTEM_PROMPT = (
    "Diese Session hat die Claude Bridge gestartet: Der Nutzer hat den Auftrag in das "
    "WhisperLoom-Widget auf seinem Telefon gesprochen, die erste Nachricht ist das Transkript "
    "der Spracherkennung. Erkennungsfehler sind moeglich — lies sinngemaess und frag nach, "
    "wenn ein einzelnes Wort ueber den Auftrag entscheidet. Der Nutzer sitzt nicht am "
    "Terminal, er verfolgt die Session per Remote Control in der Claude-App.\n\n"
    "Allererste Aktion, vor allem anderen und genau einmal: Lade das Werkzeug "
    "PushNotification (ToolSearch mit \"select:PushNotification\") und rufe es mit status "
    "\"proactive\" und der Nachricht \"Claude Bridge: Session gestartet – <Kurzfassung des "
    "Auftrags, hoechstens 60 Zeichen>\" auf. Gibt es das Werkzeug nicht, uebergehe diesen "
    "Schritt. Danach beginne direkt mit dem Auftrag."
)

#: Steuerzeichen raus, Leerraum zusammenfassen — der Name steht in der App und im Terminaltitel.
_UNSICHTBAR = re.compile(r"[\x00-\x1f\x7f-\x9f​-‏ -‮⁦-⁩]")
NAME_MAX = 48


class SessionError(RuntimeError):
    """Die Session liess sich nicht starten."""


class TooManySessions(SessionError):
    """Es laufen schon so viele Sessions, wie erlaubt sind."""


@dataclass(frozen=True)
class Options:
    model: str
    effort: str
    ultracode: bool


@dataclass(frozen=True)
class Started:
    tmux_name: str
    session_id: str
    title: str


@dataclass(frozen=True)
class Running:
    tmux_name: str
    session_id: str
    created: float


def title_for(transcript: str) -> str:
    """Name der Session in der Claude-App: der Anfang des Gesagten."""
    text = " ".join(_UNSICHTBAR.sub(" ", transcript).split())
    if len(text) > NAME_MAX:
        text = text[: NAME_MAX - 1].rstrip() + "…"
    return f"Sprachauftrag: {text}" if text else "Sprachauftrag"


def build_argv(settings: config.Settings, options: Options, session_id: str, title: str, transcript: str) -> list[str]:
    """Die Kommandozeile fuer `claude`. Das Transkript steht hinter `--` als letztes Argument:
    ein Auftrag, der mit "-" beginnt, wird so nicht als Option gelesen."""
    extra = {
        "ultracode": options.ultracode,
        # Der Push braucht diese Einstellung; wer sie global abgeschaltet hat, bekommt ihn
        # fuer Bridge-Sessions trotzdem — genau dafuer gibt es sie.
        "agentPushNotifEnabled": True,
    }
    return [
        str(settings.claude_bin),
        "--model", config.MODELS[options.model],
        "--effort", options.effort,
        "--settings", json.dumps(extra),
        "--session-id", session_id,
        "--name", title,
        "--remote-control", title,
        "--append-system-prompt", SYSTEM_PROMPT,
        "--",
        transcript,
    ]


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=config.TMUX_TIMEOUT_S,
        )
    except FileNotFoundError as e:
        raise SessionError("tmux ist nicht installiert") from e
    except subprocess.TimeoutExpired as e:
        raise SessionError(f"tmux antwortet nicht ({' '.join(args[:1])})") from e
    if check and result.returncode != 0:
        raise SessionError(f"tmux {args[0]}: {result.stderr.strip() or result.returncode}")
    return result


def list_running() -> list[Running]:
    """Alle Sessions der Bridge. Fremde tmux-Sitzungen bleiben unberuehrt; laeuft gar kein
    tmux-Server, gibt es eben keine."""
    result = _tmux("list-sessions", "-F", "#{session_name}\t#{session_created}", check=False)
    if result.returncode != 0:
        return []
    running = []
    for line in result.stdout.splitlines():
        name, _, created = line.partition("\t")
        if not name.startswith(config.SESSION_PREFIX):
            continue
        try:
            stamp = float(created)
        except ValueError:
            stamp = 0.0
        running.append(Running(name, name[len(config.SESSION_PREFIX):], stamp))
    return running


def last_activity(settings: config.Settings, session: Running) -> float:
    """Wann die Session zuletzt etwas geschrieben hat: Claude haengt jede Nachricht an
    `<claude-home>/projects/<verzeichnis>/<session-id>.jsonl` an. Fehlt die Datei noch,
    zaehlt der Start der tmux-Sitzung."""
    newest = session.created
    for transcript in (settings.claude_home / "projects").glob(f"*/{session.session_id}.jsonl"):
        try:
            newest = max(newest, transcript.stat().st_mtime)
        except OSError:
            continue
    return newest


def reap_idle(settings: config.Settings, now: float | None = None) -> list[str]:
    """Beendet Sessions, die laenger als `idle_hours` nichts mehr geschrieben haben. Der
    Verlauf bleibt erhalten — `claude --resume <session-id>` holt sie zurueck."""
    stamp = time.time() if now is None else now
    limit = settings.idle_hours * 3600
    closed = []
    for session in list_running():
        if stamp - last_activity(settings, session) < limit:
            continue
        if _tmux("kill-session", "-t", f"={session.tmux_name}", check=False).returncode == 0:
            log.info("Session %s war %.0f h still — beendet", session.tmux_name, settings.idle_hours)
            closed.append(session.tmux_name)
    return closed


def _write_task(settings: config.Settings, session_id: str, argv: list[str]) -> Path:
    tasks = settings.state_dir / "tasks"
    tasks.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = tasks / f"{session_id}.json"
    spec = {
        "argv": argv,
        "cwd": str(settings.workdir),
        "env": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **PUSH_ENV},
    }
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False)
    return path


def start(settings: config.Settings, transcript: str, options: Options) -> Started:
    """Startet eine neue Session und kehrt zurueck, sobald sie laeuft.

    Vorher werden still gewordene Sessions geschlossen; sind danach noch `max_sessions`
    offen, gibt es keine neue (TooManySessions).
    """
    reap_idle(settings)
    if len(list_running()) >= settings.max_sessions:
        raise TooManySessions(
            f"Es laufen schon {settings.max_sessions} Claude-Sessions — eine beenden und erneut senden"
        )

    session_id = str(uuid.uuid4())
    tmux_name = config.SESSION_PREFIX + session_id
    title = title_for(transcript)
    task_file = _write_task(settings, session_id, build_argv(settings, options, session_id, title, transcript))
    try:
        _tmux(
            "new-session", "-d",
            "-s", tmux_name,
            "-c", str(settings.workdir),
            "-x", str(config.TMUX_WIDTH),
            "-y", str(config.TMUX_HEIGHT),
            "--",
            sys.executable, "-I", str(LAUNCHER), str(task_file),
        )
        time.sleep(config.START_CHECK_S)
        if _tmux("has-session", "-t", f"={tmux_name}", check=False).returncode != 0:
            raise SessionError("Claude hat sich sofort wieder beendet — Aufruf von Hand pruefen (README)")
    finally:
        # Normalerweise hat launch.py sie laengst geloescht. Wenn nicht, ist der Start
        # gescheitert, und das Transkript soll nicht liegen bleiben.
        task_file.unlink(missing_ok=True)
    return Started(tmux_name, session_id, title)
