"""install.sh in einer Attrappe: eigenes HOME, systemctl, curl und loginctl ersetzt — es wird
nichts installiert und kein Dienst gestartet."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def attrappe(tmp_path):
    # "&" im Pfad und ein Home, das mit /home/chris anfaengt: beides hat die alte
    # sed-Ersetzung verdorben.
    repo = tmp_path / "a&b" / "claude-bridge"
    repo.mkdir(parents=True)
    for name in ("install.sh", ".env.example", "requirements.lock"):
        shutil.copy(REPO / name, repo / name)
    shutil.copytree(REPO / "deploy", repo / "deploy")
    # Die echte venv mitbenutzen: pip findet alles schon vor und braucht kein Netz.
    (repo / ".venv").symlink_to(REPO / ".venv")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "aufrufe.log"
    for name, body in {
        "systemctl": f'echo "systemctl $*" >> "{log}"',
        "journalctl": f'echo "journalctl $*" >> "{log}"',
        "loginctl": f'echo "loginctl $*" >> "{log}"; [ "$1" = show-user ] && echo yes; exit 0',
        "curl": 'echo \'{"status":"ok","configured":true}\'',
        "claude": "exit 0",
    }.items():
        p = stubs / name
        p.write_text(f"#!/bin/sh\n{body}\n")
        p.chmod(0o755)

    home = tmp_path / "home" / "christian"
    home.mkdir(parents=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "CLAUDE_BIN": str(home / ".local" / "bin" / "claude"),
        "PORT": "8199",
    }
    (home / ".local" / "bin").mkdir(parents=True)
    shutil.copy(stubs / "claude", home / ".local" / "bin" / "claude")
    return repo, home, env, log, stubs


def _lauf(repo, env):
    return subprocess.run(["bash", str(repo / "install.sh")], env=env, capture_output=True, text=True, timeout=120)


def _token(dotenv):
    return next(l for l in dotenv.read_text().splitlines() if l.startswith("BRIDGE_TOKEN=")).split("=", 1)[1]


def test_install_legt_env_und_unit_an(attrappe):
    repo, home, env, log, _ = attrappe
    lauf = _lauf(repo, env)
    assert lauf.returncode == 0, lauf.stderr

    dotenv = repo / ".env"
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
    token = _token(dotenv)
    assert len(token) >= 32
    assert token not in lauf.stdout and token not in lauf.stderr
    # Standard-Pfad zu claude: kein Eintrag noetig.
    assert "CLAUDE_BIN=" not in [l.split("=")[0] + "=" for l in dotenv.read_text().splitlines() if not l.startswith("#")]

    unit = (home / ".config/systemd/user/claude-bridge.service").read_text()
    assert f"WorkingDirectory={repo}\n" in unit
    assert f"EnvironmentFile=-{repo}/.env\n" in unit
    assert f"ExecStart={repo}/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8199\n" in unit
    assert f"Environment=PATH={home}/.local/bin:" in unit
    assert "@" not in unit.replace("a&b", "")

    aufrufe = log.read_text()
    assert "systemctl --user daemon-reload" in aufrufe
    assert "systemctl --user enable claude-bridge" in aufrufe
    assert "systemctl --user restart claude-bridge" in aufrufe


def test_install_merkt_sich_einen_anderen_claude_pfad(attrappe):
    repo, _, env, _, stubs = attrappe
    lauf = _lauf(repo, {**env, "CLAUDE_BIN": str(stubs / "claude")})
    assert lauf.returncode == 0, lauf.stderr
    assert f"CLAUDE_BIN={stubs / 'claude'}" in (repo / ".env").read_text().splitlines()


def test_install_laesst_vorhandene_env_in_ruhe(attrappe):
    repo, _, env, _, _ = attrappe
    (repo / ".env").write_text("BRIDGE_TOKEN=mein-eigenes-token-das-bleiben-soll-1234567\n")
    lauf = _lauf(repo, env)
    assert lauf.returncode == 0, lauf.stderr
    assert (repo / ".env").read_text() == "BRIDGE_TOKEN=mein-eigenes-token-das-bleiben-soll-1234567\n"
    assert stat.S_IMODE((repo / ".env").stat().st_mode) == 0o600


def test_install_bricht_ohne_claude_ab(attrappe):
    repo, _, env, _, _ = attrappe
    lauf = _lauf(repo, {**env, "CLAUDE_BIN": "/gibt/es/nicht/claude"})
    assert lauf.returncode != 0
    assert "claude" in lauf.stderr
    assert not (repo / ".env").exists()


def test_install_scheitert_wenn_der_dienst_nicht_antwortet(attrappe):
    repo, _, env, log, stubs = attrappe
    (stubs / "curl").write_text("#!/bin/sh\nexit 7\n")
    lauf = _lauf(repo, env)
    assert lauf.returncode == 1
    assert "antwortet nicht" in lauf.stderr
    assert "Fertig" not in lauf.stdout
    assert "journalctl --user -u claude-bridge" in log.read_text()
