"""Extraktion der ``x-enaio-*``-Header und Bereitstellung fuer Tools/Resources.

Aufrufer uebergeben Informationen zum angemeldeten Benutzer als HTTP-Header,
z. B. ``x-enaio-mail``, ``x-enaio-name`` und ``x-enaio-username``. Eine
FastMCP-Middleware liest diese Header bei jedem Tool-Aufruf und jedem
Resource-Zugriff aus und legt sie als Dict im Context-State ab. Tools und
Resources kommen ueber :func:`get_enaio_headers` an die Werte, ohne ihre
Signatur zu aendern::

    enaio = await get_enaio_headers(ctx)   # {"mail": "...", "name": "...", ...}

Die Schluessel sind die Header-Namen ohne das Praefix ``x-enaio-``, jeweils
kleingeschrieben. Bei Aufrufen ohne HTTP-Request - etwa ueber den
stdio-Transport - ist das Dict leer.
"""

import inspect
import logging
from collections.abc import Mapping

from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

# Logger-Name fest verdrahtet (nicht ``__name__``), damit configure_logging()
# das LOG_LEVEL explizit auf ihn anwenden kann (siehe logging_config.APP_LOGGERS).
logger = logging.getLogger("EnaioMCP")

# Praefix der auszuwertenden Header. Der Vergleich erfolgt case-insensitiv.
ENAIO_HEADER_PREFIX = "x-enaio-"

# Schluessel, unter dem das Dict im FastMCP-Context-State liegt. Wird
# ausschliesslich hier und in get_enaio_headers verwendet.
ENAIO_STATE_KEY = "enaio"


def extract_enaio_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Filtert die ``x-enaio-*``-Header und entfernt das Praefix aus den Namen.

    ``x-enaio-mail`` wird damit zum Schluessel ``mail``. Header ohne das Praefix
    sowie ein Header ganz ohne Namensrest (``x-enaio-``) werden uebergangen.

    :param headers: Header des eingehenden Requests (Namen beliebig gross/klein).
    :returns: Zuordnung Feldname -> Wert; leer, wenn kein passender Header dabei ist.
    """

    enaio = {}
    for name, value in headers.items():
        lowered = name.lower()
        if not lowered.startswith(ENAIO_HEADER_PREFIX):
            continue
        field = lowered[len(ENAIO_HEADER_PREFIX):]
        if field:
            enaio[field] = value
    return enaio


def enaio_placeholder_fields(enaio: Mapping[str, str]) -> dict[str, str]:
    """Bildet die Header-Felder auf Vorlagen-Platzhalter ab.

    Der Feldname wird mit grossem Anfangsbuchstaben uebernommen (``mail`` ->
    ``Mail``), passend zur Schreibweise der uebrigen Platzhalter der
    Hausvorlagen ([Betreff], [Aktenzeichen], [Datum]). Bewusst ohne feste
    Zuordnungstabelle, damit ein kuenftiger Header ``x-enaio-<feld>`` ohne
    Code-Aenderung als ``[Feld]`` zur Verfuegung steht.

    :param enaio: Ergebnis von :func:`extract_enaio_headers`.
    :returns: Zuordnung Platzhaltername (ohne eckige Klammern) -> Wert.
    """

    return {key[:1].upper() + key[1:]: value for key, value in enaio.items()}


async def _resolve(result):
    """Wertet ein Ergebnis aus, das je nach FastMCP-Version awaitable sein kann.

    Die State-API des Contexts ist ab FastMCP 3 asynchron; schlanke
    Context-Ersatzobjekte (Tests) liefern den Wert dagegen direkt.
    """

    if inspect.isawaitable(result):
        return await result
    return result


async def get_enaio_headers(ctx) -> dict[str, str]:
    """Liefert die von der Middleware abgelegten ``x-enaio-*``-Werte.

    :param ctx: Context des Tool-Aufrufs bzw. Resource-Zugriffs.
    :returns: Zuordnung Feldname -> Wert; leeres Dict, wenn keine Header
              vorliegen (z. B. stdio-Transport).
    """

    # getattr, weil ein Context ohne State-Unterstuetzung kein Fehlerfall ist.
    get_state = getattr(ctx, "get_state", None)
    if get_state is None:
        return {}
    return await _resolve(get_state(ENAIO_STATE_KEY)) or {}


class EnaioHeaderMiddleware(Middleware):
    """Legt die ``x-enaio-*``-Header jedes Aufrufs im Context-State ab."""

    async def _store(self, context: MiddlewareContext) -> None:
        """Extrahiert die Header und schreibt sie in den State des Contexts.

        :param context: Middleware-Context des aktuellen Aufrufs.
        """

        fastmcp_context = getattr(context, "fastmcp_context", None)
        if fastmcp_context is None:
            # Ohne Context (z. B. bei internen Aufrufen) gibt es nichts, worin
            # die Werte abgelegt werden koennten.
            return

        # include_all=True, da get_http_headers sonst eine eigene Auswahl trifft;
        # so bleiben kuenftige x-enaio-Header garantiert erhalten.
        enaio = extract_enaio_headers(get_http_headers(include_all=True))

        # serializable=False legt den Wert request-scoped ab. Der Default waere
        # session-scoped und wuerde die Header ueber den Aufruf hinaus im
        # State-Store halten - hier unerwuenscht, da sie zu genau diesem
        # Request gehoeren.
        await _resolve(
            fastmcp_context.set_state(ENAIO_STATE_KEY, enaio, serializable=False)
        )

        # Nur DEBUG: die vollstaendigen Header werden bereits von der
        # RequestHeaderLoggingMiddleware auf INFO protokolliert.
        logger.debug("Enaio-Headerfelder: %s", ", ".join(sorted(enaio)) or "(keine)")

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        """Stellt die Header bereit und fuehrt den Tool-Aufruf unveraendert aus."""

        await self._store(context)
        return await call_next(context)

    async def on_read_resource(self, context: MiddlewareContext, call_next: CallNext):
        """Stellt die Header bereit und fuehrt den Resource-Zugriff unveraendert aus."""

        await self._store(context)
        return await call_next(context)
