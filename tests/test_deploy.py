"""Was an der Unit nicht verloren gehen darf."""

from pathlib import Path

UNIT = (Path(__file__).resolve().parent.parent / "deploy" / "claude-bridge.service").read_text()


def test_nur_localhost():
    assert "--host 127.0.0.1" in UNIT
    assert "0.0.0.0" not in UNIT


def test_neustart_der_bridge_beendet_keine_sessions():
    assert "\nKillMode=process\n" in UNIT


def test_startet_auch_ohne_env():
    assert "EnvironmentFile=-" in UNIT


def test_claude_und_tmux_im_pfad():
    assert ".local/bin" in UNIT
