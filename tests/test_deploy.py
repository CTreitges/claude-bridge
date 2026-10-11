"""Was an der Unit nicht verloren gehen darf."""

from pathlib import Path

UNIT = (Path(__file__).resolve().parent.parent / "deploy" / "claude-bridge.service").read_text()


def test_nur_localhost():
    assert "--host 127.0.0.1 --port @PORT@" in UNIT
    assert "0.0.0.0" not in UNIT


def test_neustart_der_bridge_beendet_keine_sessions():
    assert "\nKillMode=process\n" in UNIT


def test_startet_auch_ohne_env():
    assert "EnvironmentFile=-@DIR@/.env" in UNIT


def test_startfehler_werden_sichtbar():
    assert "\nType=exec\n" in UNIT


def test_sessions_bekommen_einen_vollen_pfad():
    path = next(l for l in UNIT.splitlines() if l.startswith("Environment=PATH="))
    for teil in ("@HOME@/.local/bin", "/usr/sbin", "/usr/bin", "/bin"):
        assert teil in path.split("=", 2)[2].split(":")


def test_keine_festen_pfade_eines_nutzers():
    assert "/home/" not in UNIT
