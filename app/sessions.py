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

Danach steht das Transkript allerdings in der Kommandozeile des claude-Prozesses — und die
ist ueber /proc fuer jeden lokalen Benutzer lesbar, solange /proc ohne `hidepid` gemountet ist.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import config

log = logging.getLogger("claude-bridge")

LAUNCHER = Path(__file__).resolve().parent / "launch.py"

#: Umgebung, die jede Session zusaetzlich bekommt.
#:
#: CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK schaltet die Anwesenheitspruefung des
#: PushNotification-Werkzeugs ab. Ohne sie gilt der frisch uebergebene Startprompt als Tippen
#: am Terminal, und der Push an das Handy unterbleibt mit "Not sent — this terminal is active"
#: (gemessen 2026-10-11, 2.1.296).
#:
#: CLAUDE_BRIDGE_SESSION markiert den Prozess als Bridge-Session — etwa fuer einen Waechter,
#: der sonst jeden claude-Prozess fuer die eigene Arbeitssitzung haelt.
SESSION_ENV = {
    "CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK": "1",
    "CLAUDE_BRIDGE_SESSION": "1",
}

#: Was nie in die Umgebung von tmux oder Claude gelangen darf. Startet die Bridge den
#: tmux-Server selbst, uebernimmt er ihre Umgebung als globale — und jede spaetere Sitzung,
#: auch eine des Nutzers, haette das Token.
SECRET_ENV = ("BRIDGE_TOKEN",)

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

#: Nur so heisst eine Session der Bridge. Eine eigene tmux-Sitzung "cb-backup" bleibt fremd.
_BRIDGE_NAME = re.compile(
    "^" + re.escape(config.SESSION_PREFIX) + r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$"
)

#: Zaehlen und Starten am Stueck: zwei gleichzeitige Auftraege duerfen nicht beide unter der
#: Obergrenze durchrutschen. Der Endpunkt laeuft im Threadpool, also echt parallel.
_START_LOCK = threading.Lock()


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


def clean_env() -> dict[str, str]:
    """Die Umgebung der Bridge ohne ihre Geheimnisse."""
    return {k: v for k, v in os.environ.items() if k not in SECRET_ENV}


def _tmux(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["tmux", *args],
            capture_output=True,
            text=True,
            timeout=config.TMUX_TIMEOUT_S,
            env=clean_env(),
        )
    except FileNotFoundError as e:
        raise SessionError("tmux ist nicht installiert") from e
    except subprocess.TimeoutExpired as e:
        raise SessionError(f"tmux antwortet nicht ({args[0]})") from e
    except OSError as e:
        # Etwa fork() unter Speicherdruck (EAGAIN/ENOMEM) — die App soll es erneut versuchen.
        raise SessionError(f"tmux liess sich nicht aufrufen: {e}") from e
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
        match = _BRIDGE_NAME.match(name)
        if not match:
            continue
        try:
            stamp = float(created)
        except ValueError:
            stamp = 0.0
        running.append(Running(name, match.group(1), stamp))
    return running


def _process_records(settings: config.Settings) -> dict[str, dict]:
    """Was Claude Code zu jedem laufenden Prozess festhaelt (`<claude-home>/sessions/<pid>.json`),
    nach tmux-Sitzung: aktuelle Session-Id (sie wechselt nach /clear) und Status."""
    records: dict[str, dict] = {}
    for path in (settings.claude_home / "sessions").glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tmux = data.get("tmux") if isinstance(data, dict) else None
        if isinstance(tmux, str) and tmux:
            records[tmux.split(":", 1)[0]] = data
    return records


def _transcript_mtime(settings: config.Settings, session_id: str) -> float:
    newest = 0.0
    if not re.fullmatch(r"[0-9a-f-]{36}", session_id):
        return newest
    for transcript in (settings.claude_home / "projects").glob(f"*/{session_id}.jsonl"):
        try:
            newest = max(newest, transcript.stat().st_mtime)
        except OSError:
            continue
    return newest


def last_activity(settings: config.Settings, session: Running, record: dict | None = None) -> float:
    """Wann die Session zuletzt etwas geschrieben hat: Claude haengt jede Nachricht an
    `<claude-home>/projects/<verzeichnis>/<session-id>.jsonl` an. Nach /clear schreibt sie unter
    einer neuen Id weiter — die steht im Prozess-Eintrag. Ohne Verlauf zaehlt der Start."""
    newest = max(session.created, _transcript_mtime(settings, session.session_id))
    current = (record or {}).get("sessionId")
    if isinstance(current, str) and current != session.session_id:
        newest = max(newest, _transcript_mtime(settings, current))
    return newest


def reap_idle(settings: config.Settings, now: float | None = None) -> list[str]:
    """Beendet Sessions, die laenger als `idle_hours` nichts mehr geschrieben haben. Eine
    Session, die gerade arbeitet ("busy"), bleibt immer. Der Verlauf bleibt erhalten —
    `claude --resume <session-id>` holt sie zurueck."""
    stamp = time.time() if now is None else now
    limit = settings.idle_hours * 3600
    records = _process_records(settings)
    closed = []
    for session in list_running():
        record = records.get(session.tmux_name)
        if record and record.get("status") == "busy":
            continue
        if stamp - last_activity(settings, session, record) < limit:
            continue
        if _tmux("kill-session", "-t", f"={session.tmux_name}", check=False).returncode == 0:
            log.info("Session %s war %.0f h still — beendet", session.tmux_name, settings.idle_hours)
            closed.append(session.tmux_name)
    return closed


def _tasks_dir(settings: config.Settings) -> Path:
    tasks = settings.state_dir / "tasks"
    tasks.mkdir(mode=0o700, parents=True, exist_ok=True)
    return tasks


def _write_task(task_file: Path, settings: config.Settings, argv: list[str]) -> None:
    spec = {
        "argv": argv,
        "cwd": str(settings.workdir),
        "env": {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **SESSION_ENV},
    }
    fd = os.open(task_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(spec, f, ensure_ascii=False)


def _launcher_error(task_file: Path) -> str:
    """Was launch.py hinterlassen hat, bevor es scheiterte (sonst leer)."""
    err = task_file.with_suffix(".err")
    try:
        text = err.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    err.unlink(missing_ok=True)
    return text.splitlines()[-1] if text else ""


def _wait_for_launcher(task_file: Path) -> bool:
    """Wartet, bis launch.py die Auftragsdatei uebernommen (geloescht) hat."""
    deadline = time.monotonic() + config.LAUNCH_TIMEOUT_S
    while task_file.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def start(settings: config.Settings, transcript: str, options: Options) -> Started:
    """Startet eine neue Session und kehrt zurueck, sobald sie laeuft.

    Vorher werden still gewordene Sessions geschlossen; sind danach noch `max_sessions`
    offen, gibt es keine neue (TooManySessions). Jeder andere Fehlschlag ist ein SessionError.
    """
    session_id = str(uuid.uuid4())
    tmux_name = config.SESSION_PREFIX + session_id
    title = title_for(transcript)
    task_file: Path | None = None
    try:
        with _START_LOCK:
            reap_idle(settings)
            if len(list_running()) >= settings.max_sessions:
                raise TooManySessions(
                    f"Es laufen schon {settings.max_sessions} Claude-Sessions — eine beenden und erneut senden"
                )
            task_file = _tasks_dir(settings) / f"{session_id}.json"
            _write_task(task_file, settings, build_argv(settings, options, session_id, title, transcript))
            _tmux(
                "new-session", "-d",
                "-s", tmux_name,
                "-c", str(settings.workdir),
                "-x", str(config.TMUX_WIDTH),
                "-y", str(config.TMUX_HEIGHT),
                "--",
                sys.executable, "-I", str(LAUNCHER), str(task_file),
            )
        if not _wait_for_launcher(task_file):
            # Erst die Sitzung weg, dann die Datei: ein Starter, der knapp danach doch noch
            # liest, darf keine Session mehr anlegen, die die App nach ihrem Retry doppelt haette.
            _tmux("kill-session", "-t", f"={tmux_name}", check=False)
            raise SessionError("Claude kam nicht in Gang (Starter hat den Auftrag nicht uebernommen)")
        time.sleep(config.START_CHECK_S)
        if _tmux("has-session", "-t", f"={tmux_name}", check=False).returncode != 0:
            grund = _launcher_error(task_file)
            raise SessionError(
                "Claude hat sich sofort wieder beendet" + (f": {grund}" if grund else " — Aufruf von Hand pruefen (README)")
            )
    except OSError as e:
        # Etwa ein nicht beschreibbares State-Verzeichnis oder eine volle Platte.
        raise SessionError(f"Auftrag liess sich nicht ablegen: {e}") from e
    finally:
        # Normalerweise hat launch.py sie laengst geloescht. Wenn nicht, ist der Start
        # gescheitert, und das Transkript soll nicht liegen bleiben.
        if task_file is not None:
            task_file.unlink(missing_ok=True)
            task_file.with_suffix(".err").unlink(missing_ok=True)
    return Started(tmux_name, session_id, title)
