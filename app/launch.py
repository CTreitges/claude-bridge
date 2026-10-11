"""Laeuft IN der tmux-Sitzung und ersetzt sich durch `claude`.

Aufruf (nur durch sessions.start): `python -I launch.py <auftragsdatei>`. Die Datei enthaelt
Kommandozeile, Arbeitsverzeichnis und Umgebung als JSON; sie wird gelesen und sofort
geloescht — das ist fuer die Bridge das Zeichen, dass der Start uebernommen ist.

Scheitert etwas vor dem Wechsel zu claude, verschwindet die tmux-Sitzung samt Fehlermeldung.
Deshalb landet der Grund in `<auftragsdatei ohne .json>.err` (0600); die Bridge reicht ihn in
ihre Fehlermeldung weiter.

Nur Standardbibliothek: `-I` laedt nichts aus dem Verzeichnis des Skripts.
"""

from __future__ import annotations

import json
import os
import sys

#: Was nie an claude weitergegeben wird — auch dann nicht, wenn es ueber die globale
#: Umgebung eines tmux-Servers hereinkommt, den jemand anderes gestartet hat.
SECRET_ENV = ("BRIDGE_TOKEN",)


def _melden(path: str, fehler: BaseException) -> None:
    err = (path[:-5] if path.endswith(".json") else path) + ".err"
    try:
        fd = os.open(err, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{type(fehler).__name__}: {fehler}\n")
    except OSError:
        pass


def main(path: str) -> None:
    try:
        with open(path, encoding="utf-8") as f:
            spec = json.load(f)
    except BaseException as e:
        _melden(path, e)
        raise
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    try:
        env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
        env.update(spec["env"])
        os.chdir(spec["cwd"])
        argv = spec["argv"]
        os.execve(argv[0], argv, env)
    except BaseException as e:
        _melden(path, e)
        raise


if __name__ == "__main__":
    main(sys.argv[1])
