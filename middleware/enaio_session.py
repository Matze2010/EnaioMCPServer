"""Pruefung der verpflichtenden ``SessionID`` bei Session-Tools."""

from fastmcp.exceptions import ToolError
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


class EnaioSessionIDMiddleware(Middleware):
    """Bricht Session-Tool-Aufrufe ohne nutzbare ``SessionID`` vor der Ausfuehrung ab."""

    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        arguments = getattr(context.message, "arguments", None)
        if not has_usable_session_id(arguments):
            raise ToolError(SESSION_ID_REQUIRED_MESSAGE)

        return await call_next(context)
