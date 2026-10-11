"""HTTP-Schnittstelle der Bridge.

Ein Endpunkt: die App schickt ein Transkript, der Dienst startet damit eine neue
Claude-Code-Session in tmux — mit Remote Control, damit sie in der Claude-App erscheint.
Die App wartet nicht auf Claude, sie bekommt nur bestaetigt, dass die Session laeuft.

Gebunden wird ausschliesslich auf 127.0.0.1; nach aussen fuehrt Caddy mit TLS.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import config, sessions
from .idempotency import FailureLimiter, IdempotencyStore, RateLimiter

# Ohne eigene Konfiguration verschluckt uvicorn alles, was dieser Logger sagt. Unter systemd
# landet stdout im Journal — genau dort will man beim Fehlersuchen nachsehen.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("claude-bridge")

config.load_env()

app = FastAPI(title="WhisperLoom → Claude Code Bridge", docs_url=None, redoc_url=None, openapi_url=None)

store = IdempotencyStore(config.IDEMPOTENCY_TTL_S)
limiter = RateLimiter(config.RATE_LIMIT_PER_MIN)
auth_failures = FailureLimiter(config.AUTH_FAIL_LIMIT, config.AUTH_FAIL_WINDOW_S)

#: Kennung in der Antwort auf "Verbindung pruefen" (leeres Transkript).
BRIDGE_ID = "claude-bridge"

Model = Literal["opus", "fable", "sonnet", "haiku"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]


class Optionen(BaseModel):
    """Was das Widget-Profil mitschickt. Fehlt etwas, gilt der Standard aus `.env`."""

    model: Model | None = None
    effort: Effort | None = None
    ultracode: bool | None = None


class Task(BaseModel):
    transcript: str = Field(max_length=config.MAX_TRANSCRIPT_CHARS)
    request_id: str = Field(min_length=1, max_length=200)
    recorded_at: str = Field(default="", max_length=64)
    duration_ms: int = Field(default=0, ge=0)
    source: str = Field(default="widget", max_length=32)
    options: Optionen | None = None


class AuthFehler(Exception):
    def __init__(self, status: int, meldung: str) -> None:
        super().__init__(meldung)
        self.status = status


def fehler(status: int, meldung: str) -> JSONResponse:
    """Fehler als brauchbares JSON — die App zeigt diese Meldung an."""
    return JSONResponse(status_code=status, content={"status": "error", "detail": meldung})


def absender(request: Request) -> str:
    """Wer fragt. Hinter Caddy ist der direkte Absender immer 127.0.0.1; die echte Adresse
    steht dann als letzter Eintrag in X-Forwarded-For (den setzt Caddy selbst)."""
    direkt = request.client.host if request.client else ""
    weitergeleitet = request.headers.get("x-forwarded-for", "")
    if direkt in ("127.0.0.1", "::1") and weitergeleitet:
        return weitergeleitet.split(",")[-1].strip()
    return direkt


def pruefe_token(request: Request, authorization: Annotated[str, Header()] = "") -> None:
    """Bearer-Token in fester Zeit vergleichen. Wer zu oft daneben liegt, wird gesperrt —
    VOR dem Vergleich, damit eine Sperre auch wirklich nichts mehr probieren laesst."""
    erwartet = config.settings()
    if not erwartet.configured:
        raise AuthFehler(401, "Der Dienst hat kein (ausreichend langes) Token konfiguriert")
    wer = absender(request)
    if auth_failures.blocked(wer):
        raise AuthFehler(429, "Zu viele Fehlversuche — spaeter erneut versuchen")
    schema, _, mitgegeben = authorization.partition(" ")
    if schema.lower() != "bearer" or not hmac.compare_digest(mitgegeben.strip().encode(), erwartet.token.encode()):
        auth_failures.fail(wer)
        log.warning("Falsches oder fehlendes Token von %s", wer)
        raise AuthFehler(401, "Token fehlt oder stimmt nicht")


@app.exception_handler(AuthFehler)
async def _auth_fehler(_: Request, exc: AuthFehler) -> JSONResponse:
    return fehler(exc.status, str(exc))


@app.get("/health")
def health() -> dict[str, object]:
    """Fuer den Health-Check nach dem Deploy. Verraet nichts Geheimes."""
    return {"status": "ok", "configured": config.settings().configured}


@app.post("/v1/task", status_code=202)
def task(
    auftrag: Task,
    _: Annotated[None, Depends(pruefe_token)] = None,
) -> JSONResponse:
    # "Verbindung pruefen" in der App schickt ein leeres Transkript: 400 heisst dort
    # "erreichbar und Token stimmt". Darum kommt diese Pruefung NACH dem Token.
    if not auftrag.transcript.strip():
        # "bridge" weist den Dienst aus: so erkennt "Verbindung pruefen" in der App, wenn ein
        # Claude-Widget versehentlich auf eine andere Bridge (etwa hermes-bridge) zeigt.
        return JSONResponse(
            status_code=400,
            content={"status": "error", "detail": "Das Transkript ist leer", "bridge": BRIDGE_ID},
        )
    if not limiter.allow():
        return fehler(429, "Zu viele Auftraege — kurz warten")

    # Reservieren VOR dem Start: zwei gleichzeitige Retries derselben Id duerfen nicht
    # zwei Sessions starten.
    if not store.claim(auftrag.request_id):
        log.info("Auftrag %s war schon da — keine zweite Session", auftrag.request_id)
        return JSONResponse(
            status_code=202,
            content={"status": "accepted", "request_id": auftrag.request_id, "duplicate": True},
        )

    s = config.settings()
    wahl = auftrag.options or Optionen()
    optionen = sessions.Options(
        model=wahl.model or s.default_model,
        effort=wahl.effort or s.default_effort,
        ultracode=s.default_ultracode if wahl.ultracode is None else wahl.ultracode,
    )
    text = auftrag.transcript.strip()
    try:
        gestartet = sessions.start(s, text, optionen)
    except sessions.TooManySessions as e:
        store.release(auftrag.request_id)
        log.warning("Auftrag %s abgelehnt: %s", auftrag.request_id, e)
        return fehler(429, str(e))
    except sessions.SessionError as e:
        # Freigeben, damit die App es erneut versuchen kann — sonst waere der Auftrag
        # fuer eine Stunde gesperrt, obwohl nichts passiert ist.
        store.release(auftrag.request_id)
        log.warning("Auftrag %s nicht gestartet: %s", auftrag.request_id, e)
        return fehler(503, f"Claude-Session nicht gestartet: {e}")
    except Exception:
        # Rueckfallnetz: ohne Freigabe bekaeme der App-Retry "duplicate", und das Diktat
        # waere still verloren.
        store.release(auftrag.request_id)
        log.exception("Auftrag %s: unerwarteter Fehler beim Start", auftrag.request_id)
        return fehler(503, "Claude-Session nicht gestartet: unerwarteter Fehler (siehe Journal)")

    log.info(
        "Auftrag %s → %s (%s, %s, Ultracode %s, %d Zeichen)",
        auftrag.request_id, gestartet.tmux_name, optionen.model, optionen.effort,
        "an" if optionen.ultracode else "aus", len(text),
    )
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "request_id": auftrag.request_id,
            "session": gestartet.tmux_name,
            "title": gestartet.title,
            "model": optionen.model,
            "effort": optionen.effort,
            "ultracode": optionen.ultracode,
        },
    )
