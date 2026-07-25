"""Einfacher, prozessinterner RateLimiter fuer Uploads.

Der MCP-Server laeuft als ein einzelner Prozess; alle Tools sind ``async``.
Ein Sliding-Window-Limiter reicht damit aus, um sicherzustellen, dass nicht
mehr als ``max_per_minute`` Uploads innerhalb eines rollierenden 60-Sekunden-
Fensters erfolgen. Wird das Limit erreicht, wird der Aufruf sofort mit einer
``RateLimitExceeded``-Exception abgelehnt (kein Warten / Einreihen).

Das Modul haelt sich bewusst frei von HTTP-/MCP-/FastAPI-Abhaengigkeiten, damit
es losgeloest getestet werden kann. Das Mapping der Exception auf eine
HTTP-Antwort (z. B. 429) erfolgt an der Aufrufstelle.
"""

import asyncio
import time
from collections import deque

# Laenge des rollierenden Fensters in Sekunden.
WINDOW_SECONDS = 60.0


class RateLimitExceeded(Exception):
    """Wird geworfen, wenn das Upload-Limit pro Minute erreicht ist.

    :ivar retry_after: Sekunden, nach denen voraussichtlich wieder ein Slot
        frei ist (aufgerundet auf ganze Sekunden, mindestens 1).
    """

    def __init__(self, retry_after: int, limit: int):
        self.retry_after = retry_after
        self.limit = limit
        super().__init__(
            f"Upload-Limit von {limit} pro Minute erreicht. "
            f"Bitte in {retry_after} Sekunde(n) erneut versuchen."
        )


class RateLimiter:
    """Sliding-Window-RateLimiter fuer eine begrenzte Anzahl Aufrufe/Minute."""

    def __init__(self, max_per_minute: int):
        # Ein Wert <= 0 deaktiviert die Begrenzung (unbegrenzt).
        self.max_per_minute = max_per_minute
        self._events = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Belegt einen Slot oder wirft ``RateLimitExceeded``.

        Alte Eintraege (aelter als das Fenster) werden verworfen. Ist das
        Fenster voll, wird sofort abgelehnt; andernfalls wird der aktuelle
        Zeitpunkt vermerkt und die Methode kehrt zurueck.
        """

        if self.max_per_minute <= 0:
            return

        async with self._lock:
            now = time.monotonic()

            # Eintraege ausserhalb des Fensters entfernen.
            while self._events and (now - self._events[0]) >= WINDOW_SECONDS:
                self._events.popleft()

            if len(self._events) >= self.max_per_minute:
                # Zeit bis der aelteste Eintrag aus dem Fenster faellt.
                wait = WINDOW_SECONDS - (now - self._events[0])
                retry_after = max(1, int(wait) + (1 if wait % 1 else 0))
                raise RateLimitExceeded(retry_after=retry_after, limit=self.max_per_minute)

            self._events.append(now)
