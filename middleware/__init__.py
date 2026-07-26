"""FastMCP-Middlewares des Enaio MCP Servers.

Jede Middleware haengt in den Hooks ``on_call_tool`` und ``on_read_resource``
und greift damit automatisch fuer jedes Tool und jede Resource - auch fuer
kuenftige - ohne dass die Tool- bzw. Resource-Funktionen selbst etwas tun
muessen.

* :class:`EnaioHeaderMiddleware` extrahiert die ``x-enaio-*``-Header des
  eingehenden HTTP-Requests und legt sie im Context-State ab.
* :class:`EnaioSessionIDMiddleware` erzwingt den Parameter ``SessionID``.
* :class:`RequestHeaderLoggingMiddleware` protokolliert alle Header.

Die Registrierung erfolgt in ``EnaioMCP.py`` ueber ``mcp.add_middleware(...)``.
"""

from .enaio_headers import (
    ENAIO_HEADER_PREFIX,
    ENAIO_STATE_KEY,
    EnaioHeaderMiddleware,
    enaio_placeholder_fields,
    extract_enaio_headers,
    get_enaio_headers,
)
from .enaio_session import (
    SESSION_ID_ARGUMENT,
    SESSION_ID_DESCRIPTION,
    SESSION_ID_REQUIRED_MESSAGE,
    EnaioSessionIDMiddleware,
    has_usable_resource_session_id,
    has_usable_session_id,
    session_id_from_resource_uri,
)
from .request_logging import RequestHeaderLoggingMiddleware

__all__ = [
    "ENAIO_HEADER_PREFIX",
    "ENAIO_STATE_KEY",
    "EnaioHeaderMiddleware",
    "EnaioSessionIDMiddleware",
    "RequestHeaderLoggingMiddleware",
    "SESSION_ID_ARGUMENT",
    "SESSION_ID_DESCRIPTION",
    "SESSION_ID_REQUIRED_MESSAGE",
    "enaio_placeholder_fields",
    "extract_enaio_headers",
    "get_enaio_headers",
    "has_usable_resource_session_id",
    "has_usable_session_id",
    "session_id_from_resource_uri",
]
