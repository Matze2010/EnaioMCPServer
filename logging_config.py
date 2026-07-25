"""Zentrale Logging-Konfiguration fuer den Enaio MCP Server.

Das Level wird ueber die Umgebungsvariable ``LOG_LEVEL`` gesteuert (Default
``INFO``); zulaessig sind ``DEBUG``, ``INFO``, ``WARNING``, ``ERROR`` und
``CRITICAL`` (Gross-/Kleinschreibung egal). Ein unbekannter Wert faellt
still auf ``INFO`` zurueck.

Die Konfiguration wird auf Modulebene von ``EnaioMCP.py`` aufgerufen, damit
sie sowohl beim Start ueber ``fastmcp run ...`` (importiert das Modul) als
auch ueber ``python EnaioMCP.py`` greift.
"""

import logging
import os

# Format mit Zeitstempel, Level und Logger-Namen fuer einen lesbaren
# Betriebs-Trace.
LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"


def resolve_log_level(raw: str | None = None) -> int:
    """Ermittelt das numerische Log-Level aus ``LOG_LEVEL`` (Fallback INFO)."""
    value = (raw if raw is not None else os.environ.get("LOG_LEVEL", "INFO")).strip().upper()
    level = logging.getLevelName(value)
    # getLevelName gibt fuer unbekannte Namen einen String zurueck; nur ein
    # int ist ein gueltiges Level.
    return level if isinstance(level, int) else logging.INFO


def configure_logging() -> int:
    """Konfiguriert das prozessweite Logging und liefert das gesetzte Level.

    ``basicConfig`` ist ein No-op, sobald der Root-Logger bereits Handler hat
    (z. B. durch uvicorn/fastmcp). Daher wird das Level anschliessend auf dem
    Anwendungs-Logger ``EnaioBackend`` zusaetzlich explizit gesetzt, damit die
    ``LOG_LEVEL``-Vorgabe in jedem Startpfad wirkt.
    """
    level = resolve_log_level()
    logging.basicConfig(level=level, format=LOG_FORMAT)
    logging.getLogger("EnaioBackend").setLevel(level)
    return level
