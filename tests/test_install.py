"""install.sh in einer Attrappe: eigenes HOME, systemctl und curl ersetzt — es wird nichts
installiert und kein Dienst gestartet."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def attrappe(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in ("install.sh", ".env.example", "requirements.txt"):
        shutil.copy(REPO / name, repo / name)
    shutil.copytree(REPO / "deploy", repo / "deploy")
    # Die echte venv mitbenutzen: pip findet alles schon vor und braucht kein Netz.
    (repo / ".venv").symlink_to(REPO / ".venv")

    stubs = tmp_path / "stubs"
    stubs.mkdir()
    log = tmp_path / "aufrufe.log"
    for name, body in {
        "systemctl": f'echo "systemctl $*" >> {log}',
        "curl": 'echo \'{"status":"ok","configured":true}\'',
        "claude": "exit 0",
    }.items():
        p = stubs / name
        p.write_text(f"#!/bin/sh\n{body}\n")
        p.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PATH": f"{stubs}:{os.environ['PATH']}",
        "CLAUDE_BIN": str(stubs / "claude"),
        "PORT": "8199",
    }
    return repo, home, env, log


def _lauf(repo, env):
    return subprocess.run(["bash", str(repo / "install.sh")], env=env, capture_output=True, text=True, timeout=120)


def test_install_legt_env_und_unit_an(attrappe):
    repo, home, env, log = attrappe
    lauf = _lauf(repo, env)
    assert lauf.returncode == 0, lauf.stderr

    dotenv = repo / ".env"
    assert stat.S_IMODE(dotenv.stat().st_mode) == 0o600
    token = next(l for l in dotenv.read_text().splitlines() if l.startswith("BRIDGE_TOKEN=")).split("=", 1)[1]
    assert len(token) >= 32 and token != "hier-ein-langes-zufaelliges-token"
    assert token not in lauf.stdout and token not in lauf.stderr

    unit = (home / ".config/systemd/user/claude-bridge.service").read_text()
    assert f"WorkingDirectory={repo}\n" in unit
    assert f"EnvironmentFile=-{repo}/.env" in unit
    assert "--host 127.0.0.1 --port 8199" in unit
    assert "/home/chris" not in unit

    aufrufe = log.read_text()
    assert "systemctl --user daemon-reload" in aufrufe
    assert "systemctl --user enable --now claude-bridge" in aufrufe


def test_install_laesst_vorhandene_env_in_ruhe(attrappe):
    repo, _, env, _ = attrappe
    (repo / ".env").write_text("BRIDGE_TOKEN=mein-eigenes-token-das-bleiben-soll-1234567\n")
    lauf = _lauf(repo, env)
    assert lauf.returncode == 0, lauf.stderr
    assert (repo / ".env").read_text() == "BRIDGE_TOKEN=mein-eigenes-token-das-bleiben-soll-1234567\n"
    assert stat.S_IMODE((repo / ".env").stat().st_mode) == 0o600


def test_install_bricht_ohne_claude_ab(attrappe):
    repo, _, env, _ = attrappe
    lauf = _lauf(repo, {**env, "CLAUDE_BIN": "/gibt/es/nicht/claude"})
    assert lauf.returncode != 0
    assert "claude" in lauf.stderr
    assert not (repo / ".env").exists()
