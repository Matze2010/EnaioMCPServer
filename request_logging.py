"""Logging der HTTP-Request-Header bei Tool-Aufrufen und Resource-Zugriffen.

Die Ausgabe erfolgt ueber eine FastMCP-Middleware, die in den Hooks
``on_call_tool`` und ``on_read_resource`` haengt. Damit greift sie fuer jedes
Tool und jede Resource automatisch - auch fuer kuenftige - ohne dass die
Tool- bzw. Resource-Funktionen selbst etwas tun muessen.

Geloggt werden alle Header im Klartext (Level ``INFO``). Bei Aufrufen ohne
HTTP-Request - etwa ueber den stdio-Transport - gibt es keine Header; in diesem
Fall wird ``NO_HTTP_REQUEST`` protokolliert.
"""

import logging
from collections.abc import Mapping

from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# Logger-Name fest verdrahtet (nicht ``__name__``), damit configure_logging()
# das LOG_LEVEL explizit auf ihn anwenden kann (siehe logging_config.APP_LOGGERS).
logger = logging.getLogger("EnaioMCP")

# Platzhalter, wenn der Aufruf nicht ueber HTTP kommt (z. B. stdio).
NO_HTTP_REQUEST = "(kein HTTP-Request)"


def format_headers(headers: Mapping[str, str]) -> str:
    """Formatiert Header als sortierte ``name=wert``-Liste fuer eine Log-Zeile."""

    if not headers:
        return NO_HTTP_REQUEST
    return ", ".join(f"{name}={value}" for name, value in sorted(headers.items()))


class RequestHeaderLoggingMiddleware(Middleware):
    """Schreibt bei Tool-Aufrufen und Resource-Zugriffen die HTTP-Header ins Log."""

    def _log_headers(self, kind: str, target) -> None:
        """Schreibt eine Log-Zeile aus Zugriffsart, Ziel und allen Request-Headern.

        :param kind: Art des Zugriffs, z. B. ``"Tool-Aufruf"``.
        :param target: Name des Tools bzw. URI der Resource.
        """

        # include_all=True, da get_http_headers sonst u. a. host, accept und
        # authorization herausfiltert.
        logger.info(
            "%s %s - HTTP-Headers: %s",
            kind,
            target,
            format_headers(get_http_headers(include_all=True)),
        )

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        """Loggt Tool-Name und Header und fuehrt den Aufruf unveraendert weiter aus."""

        # Geloggt wird vor call_next, damit die Zeile auch bei einem
        # scheiternden Tool-Aufruf erscheint.
        self._log_headers("Tool-Aufruf", getattr(context.message, "name", "unbekannt"))
        return await call_next(context)

    async def on_read_resource(self, context: MiddlewareContext, call_next: CallNext):
        """Loggt Resource-URI und Header und fuehrt den Zugriff unveraendert weiter aus."""

        self._log_headers(
            "Resource-Zugriff", getattr(context.message, "uri", "unbekannt")
        )
        return await call_next(context)
