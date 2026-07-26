"""Pruefung der verpflichtenden ``SessionID`` bei Enaio-Aufrufen."""

from urllib.parse import urlparse

from fastmcp.exceptions import ResourceError, ToolError
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

SESSION_ID_ARGUMENT = "SessionID"
SESSION_ID_DESCRIPTION = (
    "Enaio SessionID des aufrufenden Clients; Voraussetzung für die Nutzung dieses Tools."
)
SESSION_ID_REQUIRED_MESSAGE = (
    "Eine Enaio SessionID ist Voraussetzung für die Nutzung dieses Tools. "
    "Bitte übergeben Sie den Parameter SessionID."
)


def has_usable_session_id(arguments: dict | None) -> bool:
    """Prueft, ob die Tool-Argumente eine nicht-leere ``SessionID`` enthalten."""

    if not arguments or SESSION_ID_ARGUMENT not in arguments:
        return False

    session_id = arguments[SESSION_ID_ARGUMENT]
    if session_id is None:
        return False
    if isinstance(session_id, str) and not session_id.strip():
        return False
    return True


def session_id_from_resource_uri(uri: str | None) -> str | None:
    """Extrahiert die ``SessionID`` aus ``document://{SessionID}/{document}/...``."""

    if not uri:
        return None

    parsed = urlparse(uri)
    if parsed.scheme != "document":
        return None

    return parsed.netloc or None


def has_usable_resource_session_id(uri: str | None) -> bool:
    """Prueft, ob eine Resource-URI eine nicht-leere ``SessionID`` enthaelt."""

    session_id = session_id_from_resource_uri(uri)
    return bool(session_id and session_id.strip())


class EnaioSessionIDMiddleware(Middleware):
    """Bricht Enaio-Aufrufe ohne nutzbare ``SessionID`` vor der Ausfuehrung ab."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        arguments = getattr(context.message, "arguments", None)
        if not has_usable_session_id(arguments):
            raise ToolError(SESSION_ID_REQUIRED_MESSAGE)

        return await call_next(context)

    async def on_read_resource(self, context: MiddlewareContext, call_next: CallNext):
        uri = getattr(context.message, "uri", None)
        if not has_usable_resource_session_id(uri):
            raise ResourceError(SESSION_ID_REQUIRED_MESSAGE)

        return await call_next(context)
