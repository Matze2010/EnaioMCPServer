"""Logging der HTTP-Request-Header bei jedem Tool-Aufruf.

Die Ausgabe erfolgt ueber eine FastMCP-Middleware, die im ``on_call_tool``-Hook
haengt. Damit greift sie fuer jedes Tool automatisch - auch fuer kuenftige - ohne
dass die Tool-Funktionen selbst etwas tun muessen.

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

# Platzhalter, wenn der Tool-Aufruf nicht ueber HTTP kommt (z. B. stdio).
NO_HTTP_REQUEST = "(kein HTTP-Request)"


def format_headers(headers: Mapping[str, str]) -> str:
    """Formatiert Header als sortierte ``name=wert``-Liste fuer eine Log-Zeile."""

    if not headers:
        return NO_HTTP_REQUEST
    return ", ".join(f"{name}={value}" for name, value in sorted(headers.items()))


class RequestHeaderLoggingMiddleware(Middleware):
    """Schreibt bei jedem Tool-Aufruf die HTTP-Header des Requests ins Log."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        """Loggt Tool-Name und Header und fuehrt den Aufruf unveraendert weiter aus."""

        tool_name = getattr(context.message, "name", "unbekannt")
        # include_all=True, da get_http_headers sonst u. a. host, accept und
        # authorization herausfiltert. Geloggt wird vor call_next, damit die
        # Zeile auch bei einem scheiternden Tool-Aufruf erscheint.
        logger.info(
            "Tool-Aufruf %s - HTTP-Headers: %s",
            tool_name,
            format_headers(get_http_headers(include_all=True)),
        )
        return await call_next(context)
