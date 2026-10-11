"""Konfiguration der Bridge.

Geheim ist nur das Token, mit dem sich die App ausweist — es steht in `.env` (chmod 600,
nie im Repo). Alles andere hat einen Standard und laesst sich in `.env` ueberschreiben.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("claude-bridge")

BASE_DIR = Path(__file__).resolve().parent.parent

#: Modelle, die die App waehlen darf, und was davon auf der Kommandozeile landet.
#:
#: Opus und Sonnet mit dem 1M-Kontext — so, wie man sie am Terminal auch nutzt. Fable hat
#: ihn ohnehin, Haiku bleibt das kleine, schnelle Modell. Gemessen 2026-10-11: alle vier
#: Werte nimmt `claude --model` an, auch zusammen mit `--effort xhigh`.
MODELS: dict[str, str] = {
    "opus": "opus[1m]",
    "fable": "fable",
    "sonnet": "sonnet[1m]",
    "haiku": "haiku",
}

#: Was `claude --effort` kennt.
EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

#: Auftraege laufen unter dem Namen `cb-<session-id>`. Die Bridge fasst nur tmux-Sitzungen
#: mit diesem Praefix an — nie die eigene Arbeitssitzung des Nutzers.
SESSION_PREFIX = "cb-"

#: Wie lange eine `request_id` als "schon gestartet" gilt. Deckt den App-Retry bei
#: wackeligem Netz ab.
IDEMPOTENCY_TTL_S = 3600

#: Jede Session ist ein voller Claude-Lauf. Ein durchgedrehtes Widget soll nicht im
#: Sekundentakt neue starten.
RATE_LIMIT_PER_MIN = 6

#: Fehlversuche mit falschem Token je Absender, bevor er eine Weile gesperrt ist. Das Token
#: oeffnet eine Shell auf diesem Rechner — Raten soll teuer sein, auch wenn es aussichtslos ist.
AUTH_FAIL_LIMIT = 10
AUTH_FAIL_WINDOW_S = 600

#: Kuerzer darf das Token nicht sein; sonst gilt der Dienst als nicht eingerichtet (401).
MIN_TOKEN_LEN = 32

#: Platzhalter, die irgendwann in einer Vorlage standen. Sie sind oeffentlich und duerfen
#: nie als Token gelten — auch nicht, wenn jemand `cp .env.example .env` macht und vergisst,
#: das Token einzutragen.
PLACEHOLDER_TOKENS = frozenset({"hier-ein-langes-zufaelliges-token"})

#: Hoechstlaenge eines Transkripts. Ein Diktat ist kein Buch; alles darueber ist ein Fehler.
MAX_TRANSCRIPT_CHARS = 20_000

#: Zeitlimit fuer jeden tmux-Aufruf.
TMUX_TIMEOUT_S = 10

#: So lange darf der Starter in der tmux-Sitzung brauchen, bis er den Auftrag uebernommen hat
#: (normal: rund 50 ms). Erst danach laeuft die Wartezeit unten.
LAUNCH_TIMEOUT_S = 10.0

#: So lange nach der Uebernahme wird nachgesehen, ob Claude noch laeuft. Ein falsches Argument
#: oder ein kaputtes Binary beendet die Sitzung sofort — das soll die App erfahren.
START_CHECK_S = 2.0

#: Groesse des Fensters, in dem Claude laeuft (es sieht ja niemand zu, solange man nicht
#: mit `tmux attach` dazukommt).
TMUX_WIDTH = 200
TMUX_HEIGHT = 50


@dataclass(frozen=True)
class Settings:
    token: str
    default_model: str
    default_effort: str
    default_ultracode: bool
    workdir: Path
    claude_bin: Path
    state_dir: Path
    claude_home: Path
    max_sessions: int
    idle_hours: float

    @property
    def configured(self) -> bool:
        return len(self.token) >= MIN_TOKEN_LEN and self.token not in PLACEHOLDER_TOKENS


def load_env(path: Path | None = None) -> None:
    """Liest `.env` in die Umgebung — schlicht, ohne Fremdbibliothek.

    Bereits gesetzte Variablen gewinnen, damit ein Test oder die systemd-Unit
    ueberschreiben kann.
    """
    env = path or BASE_DIR / ".env"
    if not env.is_file():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


def _choice(name: str, allowed, default: str) -> str:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value not in allowed:
        log.warning("%s=%r ist unbekannt, nehme %r", name, value, default)
        return default
    return value


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on", "ja")


def _number(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        number = float(value)
    except ValueError:
        log.warning("%s=%r ist keine Zahl, nehme %s", name, value, default)
        return default
    return number if number > 0 else default


def settings() -> Settings:
    home = Path.home()
    return Settings(
        token=os.environ.get("BRIDGE_TOKEN", "").strip(),
        default_model=_choice("CLAUDE_DEFAULT_MODEL", MODELS, "opus"),
        default_effort=_choice("CLAUDE_DEFAULT_EFFORT", EFFORTS, "xhigh"),
        default_ultracode=_flag("CLAUDE_DEFAULT_ULTRACODE", False),
        # Das Home ist das, was ein `claude` von Hand auch als Arbeitsverzeichnis haette —
        # und bewusst NICHT das Verzeichnis des Dienstes: dort liegt der Quelltext der Bridge.
        # Das Verzeichnis muss Claude schon einmal vertraut worden sein, sonst wartet die
        # Session unsichtbar auf den Vertrauens-Dialog.
        workdir=Path(os.environ.get("CLAUDE_WORKDIR") or home),
        claude_bin=Path(os.environ.get("CLAUDE_BIN") or home / ".local" / "bin" / "claude"),
        state_dir=Path(os.environ.get("CLAUDE_BRIDGE_STATE") or home / ".local" / "state" / "claude-bridge"),
        claude_home=Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude"),
        max_sessions=int(_number("CLAUDE_MAX_SESSIONS", 5)),
        idle_hours=_number("CLAUDE_IDLE_HOURS", 24),
    )
