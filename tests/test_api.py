"""Die HTTP-Schicht. sessions.start ist durchgehend ersetzt — hier startet NIE eine echte Session."""

import pytest
from fastapi.testclient import TestClient

from app import config, main, sessions
from conftest import TOKEN

KOPF = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def starts(monkeypatch):
    """Protokolliert, womit eine Session gestartet wuerde, statt sie zu starten."""
    protokoll = []

    def falscher_start(settings, transcript, options):
        protokoll.append({"transcript": transcript, "options": options})
        return sessions.Started("cb-1234", "1234", sessions.title_for(transcript))

    monkeypatch.setattr(sessions, "start", falscher_start)
    return protokoll


@pytest.fixture
def client():
    main.store._seen.clear()
    main.limiter._hits.clear()
    main.auth_failures._fails.clear()
    return TestClient(main.app)


def auftrag(request_id="r1", transcript="Prüfe die Caddy-Logs von heute", **extra):
    body = {
        "transcript": transcript,
        "request_id": request_id,
        "recorded_at": "2026-10-11T08:15:00+02:00",
        "duration_ms": 4200,
        "source": "widget",
    }
    body.update(extra)
    return body


# --- Zugang ---------------------------------------------------------------

def test_ohne_token_401(client, starts):
    antwort = client.post("/v1/task", json=auftrag())
    assert antwort.status_code == 401
    assert antwort.json()["detail"]
    assert starts == []


def test_falsches_token_401(client, starts):
    antwort = client.post("/v1/task", json=auftrag(), headers={"Authorization": "Bearer falsch"})
    assert antwort.status_code == 401
    assert starts == []


def test_token_ohne_bearer_schema_401(client, starts):
    antwort = client.post("/v1/task", json=auftrag(), headers={"Authorization": TOKEN})
    assert antwort.status_code == 401
    assert starts == []


def test_zu_kurzes_token_gilt_als_nicht_eingerichtet(client, starts, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "kurz")
    antwort = client.post("/v1/task", json=auftrag(), headers={"Authorization": "Bearer kurz"})
    assert antwort.status_code == 401
    assert client.get("/health").json()["configured"] is False
    assert starts == []


def test_ohne_token_in_env_401(client, starts, monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "")
    antwort = client.post("/v1/task", json=auftrag(), headers={"Authorization": "Bearer "})
    assert antwort.status_code == 401
    assert starts == []


def test_platzhalter_aus_der_vorlage_gilt_nie_als_token(client, starts, monkeypatch):
    """Der Platzhalter stand frueher in .env.example — er ist oeffentlich und lang genug."""
    platzhalter = "hier-ein-langes-zufaelliges-token"
    monkeypatch.setenv("BRIDGE_TOKEN", platzhalter)
    antwort = client.post("/v1/task", json=auftrag(transcript=""), headers={"Authorization": f"Bearer {platzhalter}"})
    assert antwort.status_code == 401
    assert client.get("/health").json()["configured"] is False


def test_vorlage_laesst_das_token_leer():
    from pathlib import Path

    vorlage = (Path(__file__).resolve().parent.parent / ".env.example").read_text()
    assert "\nBRIDGE_TOKEN=\n" in vorlage


def test_fehlversuche_sperren_den_absender(client, starts):
    falsch = {"Authorization": "Bearer falsch"}
    for _ in range(config.AUTH_FAIL_LIMIT):
        assert client.post("/v1/task", json=auftrag(), headers=falsch).status_code == 401
    # Jetzt gesperrt — auch mit dem RICHTIGEN Token, sonst liesse sich weiter raten.
    antwort = client.post("/v1/task", json=auftrag(), headers=KOPF)
    assert antwort.status_code == 429
    assert starts == []


def test_sperre_gilt_je_absender_hinter_dem_proxy(client, starts):
    """Hinter Caddy ist der direkte Absender 127.0.0.1 — gesperrt wird die Adresse aus
    X-Forwarded-For, nicht alle zugleich."""
    client = TestClient(main.app, client=("127.0.0.1", 50000))
    angreifer = {"Authorization": "Bearer falsch", "X-Forwarded-For": "203.0.113.7"}
    for _ in range(config.AUTH_FAIL_LIMIT):
        client.post("/v1/task", json=auftrag(), headers=angreifer)
    gesperrt = client.post("/v1/task", json=auftrag(), headers={**KOPF, "X-Forwarded-For": "203.0.113.7"})
    assert gesperrt.status_code == 429
    anderer = client.post("/v1/task", json=auftrag(), headers={**KOPF, "X-Forwarded-For": "198.51.100.2"})
    assert anderer.status_code == 202


def test_absender_nimmt_letzten_eintrag_von_x_forwarded_for(client):
    """Ein vorgeschobener erster Eintrag aendert nichts: den letzten setzt Caddy."""

    class Anfrage:
        client = type("C", (), {"host": "127.0.0.1"})()
        headers = {"x-forwarded-for": "1.1.1.1, 203.0.113.7"}

    assert main.absender(Anfrage()) == "203.0.113.7"


def test_x_forwarded_for_von_aussen_wird_ignoriert():
    class Anfrage:
        client = type("C", (), {"host": "203.0.113.9"})()
        headers = {"x-forwarded-for": "1.1.1.1"}

    assert main.absender(Anfrage()) == "203.0.113.9"


# --- Pruefungen vor dem Start ---------------------------------------------

def test_leeres_transkript_400_erst_nach_token(client, starts):
    """Darauf baut "Verbindung pruefen" in der App: 400 = erreichbar, Token stimmt."""
    antwort = client.post("/v1/task", json=auftrag(transcript="   "), headers=KOPF)
    assert antwort.status_code == 400
    assert starts == []
    # Und ohne Token bleibt es 401 — sonst waere 400 kein Beleg fuer ein gutes Token.
    assert client.post("/v1/task", json=auftrag(transcript="")).status_code == 401


def test_verbindung_pruefen_verbraucht_kein_rate_limit(client, starts):
    for _ in range(config.RATE_LIMIT_PER_MIN + 3):
        assert client.post("/v1/task", json=auftrag(transcript=""), headers=KOPF).status_code == 400
    assert client.post("/v1/task", json=auftrag(), headers=KOPF).status_code == 202


def test_zu_langes_transkript_422(client, starts):
    lang = "x" * (config.MAX_TRANSCRIPT_CHARS + 1)
    antwort = client.post("/v1/task", json=auftrag(transcript=lang), headers=KOPF)
    assert antwort.status_code == 422
    assert starts == []


@pytest.mark.parametrize(
    "options",
    [{"model": "gpt-5"}, {"effort": "ultra"}, {"model": "opus; rm -rf ~"}, {"ultracode": "vielleicht"}],
)
def test_unbekannte_optionen_422(client, starts, options):
    antwort = client.post("/v1/task", json=auftrag(options=options), headers=KOPF)
    assert antwort.status_code == 422
    assert starts == []


# --- Start ------------------------------------------------------------------

def test_auftrag_startet_session_mit_standard(client, starts):
    antwort = client.post("/v1/task", json=auftrag(), headers=KOPF)
    assert antwort.status_code == 202
    daten = antwort.json()
    assert daten["status"] == "accepted"
    assert daten["session"] == "cb-1234"
    assert (daten["model"], daten["effort"], daten["ultracode"]) == ("opus", "xhigh", False)
    assert starts == [
        {"transcript": "Prüfe die Caddy-Logs von heute", "options": sessions.Options("opus", "xhigh", False)}
    ]


def test_optionen_aus_dem_widget_gewinnen(client, starts):
    options = {"model": "sonnet", "effort": "max", "ultracode": True}
    antwort = client.post("/v1/task", json=auftrag(options=options), headers=KOPF)
    assert antwort.status_code == 202
    assert starts[0]["options"] == sessions.Options("sonnet", "max", True)


def test_teilweise_optionen_ergaenzt_der_standard(client, starts):
    client.post("/v1/task", json=auftrag(options={"effort": "low"}), headers=KOPF)
    assert starts[0]["options"] == sessions.Options("opus", "low", False)


def test_ultracode_false_ueberschreibt_standard_an(client, starts, monkeypatch):
    monkeypatch.setenv("CLAUDE_DEFAULT_ULTRACODE", "true")
    client.post("/v1/task", json=auftrag(request_id="a"), headers=KOPF)
    client.post("/v1/task", json=auftrag(request_id="b", options={"ultracode": False}), headers=KOPF)
    assert [s["options"].ultracode for s in starts] == [True, False]


def test_standard_aus_env(client, starts, monkeypatch):
    monkeypatch.setenv("CLAUDE_DEFAULT_MODEL", "fable")
    monkeypatch.setenv("CLAUDE_DEFAULT_EFFORT", "high")
    client.post("/v1/task", json=auftrag(), headers=KOPF)
    assert starts[0]["options"] == sessions.Options("fable", "high", False)


def test_unbekannte_felder_stoeren_nicht(client, starts):
    """Eine neuere App darf mehr schicken, als diese Bridge kennt."""
    antwort = client.post("/v1/task", json=auftrag(zukunft=1, options={"model": "haiku", "neu": "x"}), headers=KOPF)
    assert antwort.status_code == 202


def test_transkript_wird_getrimmt(client, starts):
    client.post("/v1/task", json=auftrag(transcript="  Hallo Claude \n"), headers=KOPF)
    assert starts[0]["transcript"] == "Hallo Claude"


# --- Idempotenz und Fehler ---------------------------------------------------

def test_gleiche_request_id_startet_nur_einmal(client, starts):
    erste = client.post("/v1/task", json=auftrag(), headers=KOPF)
    zweite = client.post("/v1/task", json=auftrag(), headers=KOPF)
    assert erste.status_code == zweite.status_code == 202
    assert zweite.json()["duplicate"] is True
    assert len(starts) == 1


def test_startfehler_503_und_retry_moeglich(client, monkeypatch):
    versuche = []

    def scheitert_einmal(settings, transcript, options):
        versuche.append(transcript)
        if len(versuche) == 1:
            raise sessions.SessionError("tmux ist nicht installiert")
        return sessions.Started("cb-9", "9", "Sprachauftrag")

    monkeypatch.setattr(sessions, "start", scheitert_einmal)
    erste = client.post("/v1/task", json=auftrag(), headers=KOPF)
    assert erste.status_code == 503
    assert "tmux ist nicht installiert" in erste.json()["detail"]
    # Die Id wurde freigegeben: derselbe Auftrag startet beim naechsten Versuch.
    zweite = client.post("/v1/task", json=auftrag(), headers=KOPF)
    assert zweite.status_code == 202
    assert "duplicate" not in zweite.json()


def test_unerwarteter_fehler_gibt_503_und_gibt_die_id_frei(client, monkeypatch):
    """Sonst kaeme 500, der App-Retry bekaeme "duplicate", und das Diktat waere still weg."""
    versuche = []

    def kaputt_einmal(settings, transcript, options):
        versuche.append(1)
        if len(versuche) == 1:
            raise PermissionError(13, "Permission denied")
        return sessions.Started("cb-9", "9", "Sprachauftrag")

    monkeypatch.setattr(sessions, "start", kaputt_einmal)
    erste = client.post("/v1/task", json=auftrag(), headers=KOPF)
    assert erste.status_code == 503
    assert erste.json()["status"] == "error"
    zweite = client.post("/v1/task", json=auftrag(), headers=KOPF)
    assert zweite.status_code == 202
    assert "duplicate" not in zweite.json()


def test_zu_viele_sessions_429_und_retry_moeglich(client, monkeypatch):
    def voll(settings, transcript, options):
        raise sessions.TooManySessions("Es laufen schon 5 Claude-Sessions")

    monkeypatch.setattr(sessions, "start", voll)
    antwort = client.post("/v1/task", json=auftrag(), headers=KOPF)
    assert antwort.status_code == 429
    assert "5 Claude-Sessions" in antwort.json()["detail"]
    assert main.store.claim("r1") is True  # wieder frei


def test_rate_limit(client, starts):
    for i in range(config.RATE_LIMIT_PER_MIN):
        assert client.post("/v1/task", json=auftrag(request_id=f"r{i}"), headers=KOPF).status_code == 202
    antwort = client.post("/v1/task", json=auftrag(request_id="zuviel"), headers=KOPF)
    assert antwort.status_code == 429
    assert len(starts) == config.RATE_LIMIT_PER_MIN


# --- Health -------------------------------------------------------------------

def test_health_verraet_nichts(client):
    antwort = client.get("/health")
    assert antwort.status_code == 200
    assert antwort.json() == {"status": "ok", "configured": True}
    assert TOKEN not in antwort.text


def test_keine_api_doku_oeffentlich(client):
    for pfad in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(pfad).status_code == 404


# --- Fehlversuchs-Tabelle --------------------------------------------------------

def test_fehlversuchs_tabelle_bleibt_begrenzt():
    from app.idempotency import FailureLimiter

    sperre = FailureLimiter(limit=3, window_s=600)
    for i in range(FailureLimiter.MAX_KEYS + 50):
        sperre.fail(f"10.0.{i // 256}.{i % 256}", now=1000.0)
    assert len(sperre._fails) <= FailureLimiter.MAX_KEYS


def test_alte_fehlversuche_verfallen():
    from app.idempotency import FailureLimiter

    sperre = FailureLimiter(limit=2, window_s=60)
    sperre.fail("a", now=0.0)
    sperre.fail("a", now=1.0)
    assert sperre.blocked("a", now=2.0)
    assert not sperre.blocked("a", now=62.0)
    sperre.fail("b", now=200.0)  # raeumt "a" weg
    assert "a" not in sperre._fails
