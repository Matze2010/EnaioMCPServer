"""FastMCP-Middlewares des Enaio MCP Servers.

* :class:`EnaioHeaderMiddleware` extrahiert die ``x-enaio-*``-Header des
  eingehenden HTTP-Requests und legt sie im Context-State ab.
* :class:`EnaioSessionIDMiddleware` erzwingt den Tool-Parameter ``SessionID`` im
  AuthMode ``session``.
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
    has_usable_session_id,
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
    "has_usable_session_id",
]
