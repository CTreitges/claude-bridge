"""Session-Start, Zaehlen, Aufraeumen.

Die Unit-Tests ersetzen tmux. Die Integrationstests am Ende nehmen ein ECHTES tmux, aber
mit eigenem Server (TMUX_TMPDIR auf ein Testverzeichnis) und einem falschen `claude`, das
nur seine Argumente aufschreibt — es startet nie eine echte Claude-Session.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid

import pytest

from app import config, launch, sessions


def cb():
    """Ein gueltiger Name fuer eine Bridge-Session."""
    return config.SESSION_PREFIX + str(uuid.uuid4())


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BRIDGE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-home"))
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    monkeypatch.setenv("CLAUDE_BIN", "/opt/claude/bin/claude")
    return config.settings()


OPTS = sessions.Options("opus", "xhigh", False)


# --- Kommandozeile --------------------------------------------------------------

def test_argv_traegt_optionen_und_endet_mit_transkript(settings):
    argv = sessions.build_argv(settings, OPTS, "sid-1", "Sprachauftrag: Test", "-p ist kein Schalter;")
    assert argv[0] == "/opt/claude/bin/claude"
    assert argv[argv.index("--model") + 1] == "opus[1m]"
    assert argv[argv.index("--effort") + 1] == "xhigh"
    assert argv[argv.index("--session-id") + 1] == "sid-1"
    assert argv[argv.index("--remote-control") + 1] == "Sprachauftrag: Test"
    assert argv[argv.index("--name") + 1] == "Sprachauftrag: Test"
    assert json.loads(argv[argv.index("--settings") + 1]) == {"ultracode": False, "agentPushNotifEnabled": True}
    # Das Transkript steht hinter "--" und unveraendert ganz am Ende.
    assert argv[-2:] == ["--", "-p ist kein Schalter;"]


@pytest.mark.parametrize("model,cli", sorted(config.MODELS.items()))
def test_jedes_modell_hat_einen_cli_wert(settings, model, cli):
    argv = sessions.build_argv(settings, sessions.Options(model, "low", True), "s", "t", "x")
    assert argv[argv.index("--model") + 1] == cli
    assert json.loads(argv[argv.index("--settings") + 1])["ultracode"] is True


def test_system_prompt_verlangt_den_start_push(settings):
    argv = sessions.build_argv(settings, OPTS, "s", "t", "x")
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "PushNotification" in prompt
    assert "select:PushNotification" in prompt
    assert "proactive" in prompt


def test_titel_aus_dem_transkript():
    assert sessions.title_for("Prüfe  die\nLogs") == "Sprachauftrag: Prüfe die Logs"
    assert sessions.title_for("") == "Sprachauftrag"
    assert sessions.title_for("\x1b[31mrot\x07") == "Sprachauftrag: [31mrot"
    lang = sessions.title_for("wort " * 40)
    assert lang.endswith("…")
    assert len(lang) <= len("Sprachauftrag: ") + sessions.NAME_MAX


def test_tmux_bekommt_das_token_nie(monkeypatch):
    monkeypatch.setenv("BRIDGE_TOKEN", "geheim-geheim-geheim-geheim-geheim")
    gesehen = {}

    def lauscher(cmd, **kw):
        gesehen.update(kw)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", lauscher)
    sessions._tmux("list-sessions")
    assert "BRIDGE_TOKEN" not in gesehen["env"]
    assert gesehen["env"]["PATH"] == os.environ["PATH"]


def test_tmux_fork_fehler_wird_session_error(monkeypatch):
    def kein_fork(cmd, **kw):
        raise BlockingIOError(11, "Resource temporarily unavailable")

    monkeypatch.setattr(subprocess, "run", kein_fork)
    with pytest.raises(sessions.SessionError, match="nicht aufrufen"):
        sessions._tmux("list-sessions")


# --- Start mit ersetztem tmux ----------------------------------------------------

class FalschesTmux:
    """Spielt tmux nach: list-sessions, new-session, has-session, kill-session. new-session
    spielt auch den Starter: er nimmt die Auftragsdatei an sich (loescht sie)."""

    def __init__(self, laufend=(), stirbt_sofort=False, new_scheitert=False, starter_haengt=False):
        self.laufend = dict(laufend)  # name -> created
        self.stirbt_sofort = stirbt_sofort
        self.new_scheitert = new_scheitert
        self.starter_haengt = starter_haengt
        self.aufrufe = []
        self.auftraege = []

    def __call__(self, *args, check=True):
        self.aufrufe.append(args)
        befehl = args[0]
        if befehl == "list-sessions":
            out = "".join(f"{n}\t{int(c)}\n" for n, c in self.laufend.items())
            return subprocess.CompletedProcess(args, 0, out, "")
        if befehl == "new-session":
            if self.new_scheitert:
                raise sessions.SessionError("tmux new-session: kaputt")
            name = args[args.index("-s") + 1]
            datei = args[-1]
            with open(datei, encoding="utf-8") as f:
                self.auftraege.append(json.load(f))
            if not self.starter_haengt:
                os.unlink(datei)
            if not self.stirbt_sofort:
                self.laufend[name] = time.time()
            return subprocess.CompletedProcess(args, 0, "", "")
        if befehl in ("has-session", "kill-session"):
            name = args[args.index("-t") + 1].lstrip("=")
            da = name in self.laufend
            if befehl == "kill-session" and da:
                del self.laufend[name]
            return subprocess.CompletedProcess(args, 0 if da else 1, "", "")
        raise AssertionError(f"unerwarteter tmux-Aufruf {args}")


@pytest.fixture
def ohne_wartezeit(monkeypatch):
    monkeypatch.setattr(config, "START_CHECK_S", 0)


def test_start_legt_tmux_sitzung_an(settings, monkeypatch, ohne_wartezeit):
    tmux = FalschesTmux()
    monkeypatch.setattr(sessions, "_tmux", tmux)
    gestartet = sessions.start(settings, "Bau mir eine Bridge; jetzt", OPTS)
    assert gestartet.tmux_name == "cb-" + gestartet.session_id
    neu = next(a for a in tmux.aufrufe if a[0] == "new-session")
    assert neu[neu.index("-c") + 1] == str(settings.workdir)
    assert neu[neu.index("-s") + 1] == gestartet.tmux_name
    # Hinter "--" laeuft der Starter mit der Auftragsdatei — sonst nichts.
    befehl = neu[neu.index("--") + 1:]
    assert befehl[1:3] == ("-I", str(sessions.LAUNCHER))
    assert len(befehl) == 4


def test_transkript_steht_nie_auf_der_tmux_zeile(settings, monkeypatch, ohne_wartezeit):
    """tmux liest ein Argument, das auf ';' endet, als Befehlsende — das Transkript
    koennte sonst tmux-Befehle einschleusen."""
    tmux = FalschesTmux()
    monkeypatch.setattr(sessions, "_tmux", tmux)
    sessions.start(settings, "Auftrag mit Semikolon; kill-server;", OPTS)
    for aufruf in tmux.aufrufe:
        assert not any("Semikolon" in arg for arg in aufruf)


def test_auftragsdatei_ist_privat_und_vollstaendig(settings, monkeypatch, ohne_wartezeit):
    gesehen = {}

    class MitBlick(FalschesTmux):
        def __call__(self, *args, check=True):
            if args[0] == "new-session":
                datei = args[-1]
                gesehen["modus"] = stat.S_IMODE(os.stat(datei).st_mode)
                gesehen["ordner"] = stat.S_IMODE(os.stat(os.path.dirname(datei)).st_mode)
            return super().__call__(*args, check=check)

    tmux = MitBlick()
    monkeypatch.setattr(sessions, "_tmux", tmux)
    gestartet = sessions.start(settings, "Hallo Claude", OPTS)
    assert gesehen["modus"] == 0o600
    assert gesehen["ordner"] == 0o700
    spec = tmux.auftraege[0]
    assert spec["argv"] == sessions.build_argv(settings, OPTS, gestartet.session_id, gestartet.title, "Hallo Claude")
    assert spec["cwd"] == str(settings.workdir)
    assert spec["env"]["CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK"] == "1"
    assert spec["env"]["CLAUDE_BRIDGE_SESSION"] == "1"
    assert spec["env"]["PATH"]
    assert "BRIDGE_TOKEN" not in spec["env"]
    # Nach dem Start liegt kein Transkript mehr herum.
    assert list((settings.state_dir / "tasks").iterdir()) == []


def test_sofort_beendete_session_ist_ein_fehler(settings, monkeypatch, ohne_wartezeit):
    monkeypatch.setattr(sessions, "_tmux", FalschesTmux(stirbt_sofort=True))
    with pytest.raises(sessions.SessionError, match="sofort"):
        sessions.start(settings, "Hallo", OPTS)
    assert list((settings.state_dir / "tasks").iterdir()) == []


def test_haengender_starter_wird_abgeraeumt(settings, monkeypatch, ohne_wartezeit):
    """Uebernimmt der Starter den Auftrag nicht, darf keine Session uebrig bleiben, die nach
    dem Retry der App eine zweite waere."""
    monkeypatch.setattr(config, "LAUNCH_TIMEOUT_S", 0.2)
    tmux = FalschesTmux(starter_haengt=True)
    monkeypatch.setattr(sessions, "_tmux", tmux)
    with pytest.raises(sessions.SessionError, match="nicht in Gang"):
        sessions.start(settings, "Hallo", OPTS)
    assert [n for n in tmux.laufend if n.startswith("cb-")] == []
    assert list((settings.state_dir / "tasks").iterdir()) == []


def test_tmux_fehler_raeumt_auftragsdatei_weg(settings, monkeypatch, ohne_wartezeit):
    monkeypatch.setattr(sessions, "_tmux", FalschesTmux(new_scheitert=True))
    with pytest.raises(sessions.SessionError):
        sessions.start(settings, "Hallo", OPTS)
    assert list((settings.state_dir / "tasks").iterdir()) == []


def test_nicht_beschreibbarer_state_ist_ein_session_error(settings, monkeypatch, ohne_wartezeit, tmp_path):
    """Sonst gaebe es 500, die request_id bliebe belegt, und das Diktat ginge still verloren."""
    blockiert = tmp_path / "blockiert"
    blockiert.write_text("keine Verzeichnis")
    monkeypatch.setenv("CLAUDE_BRIDGE_STATE", str(blockiert))
    monkeypatch.setattr(sessions, "_tmux", FalschesTmux())
    with pytest.raises(sessions.SessionError, match="ablegen"):
        sessions.start(config.settings(), "Hallo", OPTS)


def test_obergrenze(settings, monkeypatch, ohne_wartezeit):
    jetzt = time.time()
    laufend = {cb(): jetzt for _ in range(settings.max_sessions)}
    tmux = FalschesTmux(laufend)
    monkeypatch.setattr(sessions, "_tmux", tmux)
    with pytest.raises(sessions.TooManySessions):
        sessions.start(settings, "Hallo", OPTS)
    assert not any(a[0] == "new-session" for a in tmux.aufrufe)


def test_fremde_sitzungen_zaehlen_nicht(settings, monkeypatch, ohne_wartezeit):
    jetzt = time.time()
    fremd = {"claude": jetzt, "claude2": jetzt, "arbeit": jetzt, "cbx": jetzt, "cb-backup": jetzt, "cb-*": jetzt}
    laufend = {**fremd, **{cb(): jetzt for _ in range(settings.max_sessions - 1)}}
    tmux = FalschesTmux(laufend)
    monkeypatch.setattr(sessions, "_tmux", tmux)
    sessions.start(settings, "Hallo", OPTS)
    assert all(name in tmux.laufend for name in fremd)


def test_ohne_tmux_server_gibt_es_keine_sessions(monkeypatch):
    monkeypatch.setattr(
        sessions, "_tmux",
        lambda *a, check=True: subprocess.CompletedProcess(a, 1, "", "no server running"),
    )
    assert sessions.list_running() == []


# --- Aufraeumen ------------------------------------------------------------------

def _transkript(settings, session_id, mtime):
    pfad = settings.claude_home / "projects" / "-home-x" / f"{session_id}.jsonl"
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text("{}\n")
    os.utime(pfad, (mtime, mtime))


def _prozess(settings, tmux_name, session_id, status="idle", pid=4242):
    pfad = settings.claude_home / "sessions" / f"{pid}.json"
    pfad.parent.mkdir(parents=True, exist_ok=True)
    pfad.write_text(json.dumps({"pid": pid, "sessionId": session_id, "status": status, "tmux": f"{tmux_name}:@3.%3"}))


def test_stille_sessions_werden_geschlossen(settings, monkeypatch):
    jetzt = 1_000_000_000.0
    stunde = 3600
    alt, aktiv, frisch = cb(), cb(), cb()
    tmux = FalschesTmux({
        alt: jetzt - 48 * stunde,       # alt und seitdem still
        aktiv: jetzt - 48 * stunde,     # alt, aber eben noch geschrieben
        frisch: jetzt - 1 * stunde,     # noch ohne Verlauf, aber jung
        "claude": jetzt - 999 * stunde,  # fremd — nie anfassen
        "cb-backup": jetzt - 999 * stunde,  # fremd, obwohl mit cb- vorn
    })
    monkeypatch.setattr(sessions, "_tmux", tmux)
    _transkript(settings, alt[3:], jetzt - 30 * stunde)
    _transkript(settings, aktiv[3:], jetzt - 10)
    geschlossen = sessions.reap_idle(settings, now=jetzt)
    assert geschlossen == [alt]
    assert set(tmux.laufend) == {aktiv, frisch, "claude", "cb-backup"}
    # Exakter Name ("="), kein Praefix-Treffer auf eine andere Sitzung.
    assert ("kill-session", "-t", f"={alt}") in tmux.aufrufe


def test_nach_clear_zaehlt_die_neue_session_id(settings, monkeypatch):
    """/clear gibt der Session eine neue Id — sie schreibt dann in eine andere Datei weiter."""
    jetzt = 1_000_000_000.0
    name = cb()
    tmux = FalschesTmux({name: jetzt - 48 * 3600})
    monkeypatch.setattr(sessions, "_tmux", tmux)
    _transkript(settings, name[3:], jetzt - 30 * 3600)
    neue_id = str(uuid.uuid4())
    _transkript(settings, neue_id, jetzt - 60)
    _prozess(settings, name, neue_id)
    assert sessions.reap_idle(settings, now=jetzt) == []
    assert name in tmux.laufend


def test_arbeitende_session_bleibt_immer(settings, monkeypatch):
    jetzt = 1_000_000_000.0
    name = cb()
    tmux = FalschesTmux({name: jetzt - 99 * 3600})
    monkeypatch.setattr(sessions, "_tmux", tmux)
    _prozess(settings, name, name[3:], status="busy")
    assert sessions.reap_idle(settings, now=jetzt) == []


def test_kaputte_prozess_eintraege_stoeren_nicht(settings, monkeypatch):
    jetzt = 1_000_000_000.0
    name = cb()
    monkeypatch.setattr(sessions, "_tmux", FalschesTmux({name: jetzt - 99 * 3600}))
    ordner = settings.claude_home / "sessions"
    ordner.mkdir(parents=True)
    (ordner / "1.json").write_text("{kaputt")
    (ordner / "2.json").write_text("[]")
    (ordner / "3.json").write_text(json.dumps({"tmux": name, "sessionId": "../../etc/passwd"}))
    assert sessions.reap_idle(settings, now=jetzt) == [name]


def test_start_raeumt_vorher_auf_und_hat_dann_platz(settings, monkeypatch, ohne_wartezeit):
    alt = time.time() - (settings.idle_hours + 1) * 3600
    tmux = FalschesTmux({cb(): alt for _ in range(settings.max_sessions)})
    monkeypatch.setattr(sessions, "_tmux", tmux)
    sessions.start(settings, "Hallo", OPTS)
    assert len([n for n in tmux.laufend if n.startswith("cb-")]) == 1


# --- Starter ------------------------------------------------------------------------

def test_launcher_liest_loescht_und_ersetzt_sich(tmp_path, monkeypatch):
    datei = tmp_path / "auftrag.json"
    spec = {"argv": ["/bin/claude", "--", "Hallo; welt"], "cwd": str(tmp_path), "env": {"X_TEST": "1"}}
    datei.write_text(json.dumps(spec), encoding="utf-8")
    monkeypatch.setenv("BRIDGE_TOKEN", "geheim-geheim-geheim-geheim-geheim")
    gesehen = {}
    monkeypatch.setattr(os, "chdir", lambda p: gesehen.setdefault("cwd", p))
    monkeypatch.setattr(os, "execve", lambda prog, argv, env: gesehen.update(prog=prog, argv=argv, env=env))
    launch.main(str(datei))
    assert not datei.exists()
    assert gesehen["prog"] == "/bin/claude"
    assert gesehen["argv"] == spec["argv"]
    assert gesehen["cwd"] == str(tmp_path)
    assert gesehen["env"]["X_TEST"] == "1"
    assert "PATH" in gesehen["env"]
    assert "BRIDGE_TOKEN" not in gesehen["env"]


def test_launcher_hinterlaesst_den_grund(tmp_path):
    datei = tmp_path / "auftrag.json"
    datei.write_text(json.dumps({"argv": [str(tmp_path / "gibt-es-nicht")], "cwd": str(tmp_path), "env": {}}))
    with pytest.raises(FileNotFoundError):
        launch.main(str(datei))
    err = tmp_path / "auftrag.err"
    assert "FileNotFoundError" in err.read_text()
    assert stat.S_IMODE(err.stat().st_mode) == 0o600
    assert not datei.exists()


def test_launcher_meldet_kaputte_datei(tmp_path):
    datei = tmp_path / "auftrag.json"
    datei.write_text("{kaputt")
    with pytest.raises(ValueError):
        launch.main(str(datei))
    assert "JSONDecodeError" in (tmp_path / "auftrag.err").read_text()
    assert not datei.exists()


# --- Integration mit echtem tmux ------------------------------------------------------

FAKE_CLAUDE = """#!/bin/sh
out="$(dirname "$0")/argv.bin"
printf '%s\\0' "$@" > "$out.tmp"
pwd > "$(dirname "$0")/cwd.txt"
env > "$(dirname "$0")/env.txt"
mv "$out.tmp" "$out"
exec sleep 30
"""


@pytest.fixture
def echtes_tmux(tmp_path, monkeypatch):
    if not shutil.which("tmux"):
        pytest.skip("tmux fehlt")
    sockets = tmp_path / "tmux"
    sockets.mkdir()
    # Eigener Server: nie die Sitzungen des Nutzers anfassen. TMUX zeigt sonst auf den
    # Server, in dem der Testlauf gerade selbst laeuft.
    monkeypatch.setenv("TMUX_TMPDIR", str(sockets))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(config, "START_CHECK_S", 0.5)
    monkeypatch.setenv("CLAUDE_BRIDGE_STATE", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_WORKDIR", str(tmp_path))
    yield
    subprocess.run(["tmux", "kill-server"], capture_output=True)


def _fake_claude(tmp_path, body=FAKE_CLAUDE):
    ordner = tmp_path / "bin"
    ordner.mkdir(exist_ok=True)
    programm = ordner / "claude"
    programm.write_text(body)
    programm.chmod(0o755)
    return programm


def _warte_auf(pfad, sekunden=10):
    ende = time.time() + sekunden
    while time.time() < ende:
        if pfad.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"{pfad} kam nicht")


def test_echter_start_reicht_transkript_unversehrt_durch(tmp_path, monkeypatch, echtes_tmux):
    programm = _fake_claude(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", str(programm))
    s = config.settings()
    heikel = "-p Zeile eins\nZeile \"zwei\" mit $HOME und `id`; kill-server;"
    gestartet = sessions.start(s, heikel, sessions.Options("sonnet", "max", True))

    _warte_auf(programm.parent / "argv.bin")
    argv = (programm.parent / "argv.bin").read_bytes().split(b"\0")[:-1]
    erwartet = sessions.build_argv(s, sessions.Options("sonnet", "max", True), gestartet.session_id, gestartet.title, heikel)
    assert [a.decode() for a in argv] == erwartet[1:]
    assert (programm.parent / "cwd.txt").read_text().strip() == str(tmp_path)
    env = (programm.parent / "env.txt").read_text()
    assert "CLAUDE_CODE_DISABLE_NOTIFICATION_PRESENCE_CHECK=1" in env
    assert "CLAUDE_BRIDGE_SESSION=1" in env
    # Die Session laeuft, der Auftrag liegt nirgends mehr als Datei.
    assert [r.tmux_name for r in sessions.list_running()] == [gestartet.tmux_name]
    assert list((s.state_dir / "tasks").iterdir()) == []


def test_token_gelangt_nicht_in_den_tmux_server(tmp_path, monkeypatch, echtes_tmux):
    """Startet die Bridge den tmux-Server selbst, uebernimmt er ihre Umgebung als globale —
    jede spaetere Sitzung, auch eine des Nutzers, haette sonst das Token."""
    programm = _fake_claude(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", str(programm))
    monkeypatch.setenv("BRIDGE_TOKEN", "geheim-geheim-geheim-geheim-geheim")
    sessions.start(config.settings(), "Hallo", OPTS)
    _warte_auf(programm.parent / "argv.bin")
    assert "BRIDGE_TOKEN" not in (programm.parent / "env.txt").read_text()
    global_env = subprocess.run(["tmux", "show-environment", "-g"], capture_output=True, text=True).stdout
    assert "BRIDGE_TOKEN" not in global_env


def test_echter_start_merkt_sofortigen_abbruch(tmp_path, monkeypatch, echtes_tmux):
    programm = _fake_claude(tmp_path, "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("CLAUDE_BIN", str(programm))
    with pytest.raises(sessions.SessionError, match="sofort"):
        sessions.start(config.settings(), "Hallo", OPTS)
    assert sessions.list_running() == []


def test_echter_start_nennt_den_grund(tmp_path, monkeypatch, echtes_tmux):
    monkeypatch.setenv("CLAUDE_BIN", str(tmp_path / "gibt-es-nicht"))
    with pytest.raises(sessions.SessionError, match="FileNotFoundError"):
        sessions.start(config.settings(), "Hallo", OPTS)
    assert list((tmp_path / "state" / "tasks").iterdir()) == []


def test_langsamer_starter_wird_abgewartet(tmp_path, monkeypatch, echtes_tmux):
    """Frueher galt eine feste Wartezeit: kam der Starter erst danach, war der Auftrag schon
    geloescht, die App hatte aber 202 bekommen."""
    programm = _fake_claude(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", str(programm))
    langsam = tmp_path / "langsames-python"
    langsam.write_text(f"#!/bin/sh\nsleep 1.5\nexec {sys.executable} \"$@\"\n")
    langsam.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(langsam))
    monkeypatch.setattr(config, "START_CHECK_S", 0.2)
    gestartet = sessions.start(config.settings(), "Hallo", OPTS)
    _warte_auf(programm.parent / "argv.bin")
    assert [r.tmux_name for r in sessions.list_running()] == [gestartet.tmux_name]


def test_obergrenze_haelt_auch_bei_gleichzeitigen_auftraegen(tmp_path, monkeypatch, echtes_tmux):
    programm = _fake_claude(tmp_path)
    monkeypatch.setenv("CLAUDE_BIN", str(programm))
    monkeypatch.setenv("CLAUDE_MAX_SESSIONS", "1")
    s = config.settings()
    ergebnisse = []

    def los():
        try:
            sessions.start(s, "Hallo", OPTS)
            ergebnisse.append("ok")
        except sessions.TooManySessions:
            ergebnisse.append("voll")

    faeden = [threading.Thread(target=los) for _ in range(4)]
    for f in faeden:
        f.start()
    for f in faeden:
        f.join()
    assert sorted(ergebnisse) == ["ok", "voll", "voll", "voll"]
    assert len(sessions.list_running()) == 1
