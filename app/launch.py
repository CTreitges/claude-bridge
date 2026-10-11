"""Laeuft IN der tmux-Sitzung und ersetzt sich durch `claude`.

Aufruf (nur durch sessions.start): `python -I launch.py <auftragsdatei>`. Die Datei enthaelt
Kommandozeile, Arbeitsverzeichnis und Umgebung als JSON; sie wird gelesen und sofort
geloescht — das Transkript steht danach nur noch dort, wo Claude seinen Verlauf ablegt.

Nur Standardbibliothek: `-I` laedt nichts aus dem Verzeichnis des Skripts.
"""

from __future__ import annotations

import json
import os
import sys


def main(path: str) -> None:
    with open(path, encoding="utf-8") as f:
        spec = json.load(f)
    os.unlink(path)
    env = dict(os.environ)
    env.update(spec["env"])
    os.chdir(spec["cwd"])
    argv = spec["argv"]
    os.execve(argv[0], argv, env)


if __name__ == "__main__":
    main(sys.argv[1])
