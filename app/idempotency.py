"""Merkt sich gestartete Auftraege, damit ein App-Retry keine zweite Session startet, und
bremst zu viele Anfragen."""

from __future__ import annotations

import threading
import time


class IdempotencyStore:
    """`request_id` -> Ablaufzeitpunkt. Klein genug fuer den Speicher, gross genug fuer
    einen Funkloch-Retry.

    Bewusst ohne Persistenz: ein Neustart des Dienstes ist selten genug, und ein doppelt
    zugestellter Auftrag danach ist ein kleineres Uebel als eine Datei, die mitwaechst.
    """

    def __init__(self, ttl_s: int) -> None:
        self._ttl_s = ttl_s
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def claim(self, request_id: str, now: float | None = None) -> bool:
        """Reserviert [request_id]. `True` = neu (bitte bearbeiten), `False` = schon bekannt.

        Die Reservierung passiert VOR der Bearbeitung: zwei gleichzeitige Anfragen mit
        derselben Id duerfen nicht beide durchlaufen.
        """
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._purge(stamp)
            if request_id in self._seen:
                return False
            self._seen[request_id] = stamp + self._ttl_s
            return True

    def release(self, request_id: str) -> None:
        """Gibt eine Reservierung zurueck — nach einem Fehlschlag, damit die App es
        erneut versuchen kann."""
        with self._lock:
            self._seen.pop(request_id, None)

    def _purge(self, now: float) -> None:
        abgelaufen = [k for k, ende in self._seen.items() if ende <= now]
        for k in abgelaufen:
            del self._seen[k]

    def __len__(self) -> int:
        with self._lock:
            self._purge(time.monotonic())
            return len(self._seen)


class RateLimiter:
    """Einfaches Zeitfenster: hoechstens [limit] Anfragen je 60 Sekunden."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._hits: list[float] = []
        self._lock = threading.Lock()

    def allow(self, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._hits = [t for t in self._hits if stamp - t < 60.0]
            if len(self._hits) >= self._limit:
                return False
            self._hits.append(stamp)
            return True


class FailureLimiter:
    """Zaehlt Fehlversuche je Absender: nach [limit] Fehlern in [window_s] Sekunden ist er
    gesperrt, bis die aeltesten Fehler aus dem Fenster fallen."""

    def __init__(self, limit: int, window_s: float) -> None:
        self._limit = limit
        self._window_s = window_s
        self._fails: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, key: str, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else now
        with self._lock:
            return len(self._recent(key, stamp)) >= self._limit

    def fail(self, key: str, now: float | None = None) -> None:
        stamp = time.monotonic() if now is None else now
        with self._lock:
            self._fails[key] = self._recent(key, stamp) + [stamp]
            # Alte Absender vergessen, damit die Tabelle nicht mitwaechst.
            for other in [k for k, v in self._fails.items() if not v or stamp - v[-1] >= self._window_s]:
                del self._fails[other]

    def _recent(self, key: str, now: float) -> list[float]:
        return [t for t in self._fails.get(key, []) if now - t < self._window_s]
