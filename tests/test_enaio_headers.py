"""Tests fuer die Extraktion der x-enaio-Header und deren Bereitstellung."""

import logging
from types import SimpleNamespace

import pytest

from middleware import enaio_headers


class _FastMCPContext:
    """Minimaler Ersatz fuer den FastMCP-Context (nur der State wird genutzt).

    Die State-API ist - wie ab FastMCP 3 - asynchron.
    """

    def __init__(self, state=None):
        self.state = dict(state or {})
        self.scopes = []

    async def set_state(self, key, value, *, serializable=True):
        self.state[key] = value
        self.scopes.append(serializable)

    async def get_state(self, key):
        return self.state.get(key)


class _SyncStateContext:
    """Context mit synchroner State-API (FastMCP 2)."""

    def __init__(self, state):
        self.state = dict(state)

    def get_state(self, key):
        return self.state.get(key)


def _context(fastmcp_context=None, tool_name="create_case_document"):
    """Middleware-Context-Ersatz mit ``message`` und ``fastmcp_context``."""
    return SimpleNamespace(
        message=SimpleNamespace(name=tool_name, uri="document://132887/fulltext"),
        fastmcp_context=fastmcp_context,
    )


def test_extract_filters_and_strips_prefix():
    extracted = enaio_headers.extract_enaio_headers(
        {
            "x-enaio-mail": "mathias.gisch@me.com",
            "x-enaio-name": "admin-gisch",
            "x-enaio-username": "3ff37dc9-5f6f-42f2-8363-5a92423008a3",
            "authorization": "Bearer test123",
            "host": "enaio.test",
        }
    )

    assert extracted == {
        "mail": "mathias.gisch@me.com",
        "name": "admin-gisch",
        "username": "3ff37dc9-5f6f-42f2-8363-5a92423008a3",
    }


def test_extract_is_case_insensitive():
    extracted = enaio_headers.extract_enaio_headers({"X-Enaio-Mail": "a@b.de"})

    # Header-Namen sind laut RFC case-insensitiv; der Schluessel ist immer klein.
    assert extracted == {"mail": "a@b.de"}


def test_extract_ignores_prefix_without_field():
    assert enaio_headers.extract_enaio_headers({"x-enaio-": "leer"}) == {}


def test_extract_without_headers():
    assert enaio_headers.extract_enaio_headers({}) == {}


def test_placeholder_fields_capitalize_first_letter():
    fields = enaio_headers.enaio_placeholder_fields(
        {"mail": "a@b.de", "name": "admin-gisch", "user-id": "42"}
    )

    assert fields == {"Mail": "a@b.de", "Name": "admin-gisch", "User-id": "42"}


def test_placeholder_fields_without_headers():
    assert enaio_headers.enaio_placeholder_fields({}) == {}


@pytest.mark.parametrize("hook", ["on_call_tool", "on_read_resource"])
async def test_hook_stores_headers_in_state(monkeypatch, hook):
    sentinel = object()
    seen = []

    async def call_next(context):
        seen.append(context)
        return sentinel

    monkeypatch.setattr(
        enaio_headers,
        "get_http_headers",
        lambda include_all=False: {
            "x-enaio-mail": "mathias.gisch@me.com",
            "authorization": "Bearer test123",
        },
    )

    fastmcp_context = _FastMCPContext()
    context = _context(fastmcp_context)
    middleware = enaio_headers.EnaioHeaderMiddleware()
    result = await getattr(middleware, hook)(context, call_next)

    # Der Aufruf wird unveraendert weitergereicht.
    assert result is sentinel
    assert seen == [context]

    assert fastmcp_context.state == {
        enaio_headers.ENAIO_STATE_KEY: {"mail": "mathias.gisch@me.com"}
    }
    # Request-scoped ablegen: die Header gehoeren nur zu diesem Aufruf.
    assert fastmcp_context.scopes == [False]


@pytest.mark.parametrize("hook", ["on_call_tool", "on_read_resource"])
async def test_hooks_request_all_headers(monkeypatch, hook):
    """Ohne ``include_all`` trifft get_http_headers eine eigene Auswahl."""

    calls = []

    async def call_next(context):
        return None

    def fake_get_http_headers(include_all=False):
        calls.append(include_all)
        return {}

    monkeypatch.setattr(enaio_headers, "get_http_headers", fake_get_http_headers)

    middleware = enaio_headers.EnaioHeaderMiddleware()
    await getattr(middleware, hook)(_context(_FastMCPContext()), call_next)

    assert calls == [True]


async def test_hook_without_http_request(monkeypatch, caplog):
    """Beim stdio-Transport gibt es keine Header - der Aufruf laeuft trotzdem durch."""

    async def call_next(context):
        return "ok"

    monkeypatch.setattr(enaio_headers, "get_http_headers", lambda include_all=False: {})

    fastmcp_context = _FastMCPContext()
    middleware = enaio_headers.EnaioHeaderMiddleware()
    with caplog.at_level(logging.DEBUG, logger="EnaioMCP"):
        result = await middleware.on_call_tool(_context(fastmcp_context), call_next)

    assert result == "ok"
    assert fastmcp_context.state == {enaio_headers.ENAIO_STATE_KEY: {}}
    assert "Enaio-Headerfelder: (keine)" in caplog.text


async def test_hook_without_fastmcp_context(monkeypatch):
    """Ohne Context gibt es keinen State - der Aufruf darf nicht scheitern."""

    async def call_next(context):
        return "ok"

    monkeypatch.setattr(
        enaio_headers,
        "get_http_headers",
        lambda include_all=False: {"x-enaio-mail": "a@b.de"},
    )

    middleware = enaio_headers.EnaioHeaderMiddleware()

    assert await middleware.on_call_tool(_context(None), call_next) == "ok"


async def test_get_enaio_headers_returns_state():
    ctx = _FastMCPContext({enaio_headers.ENAIO_STATE_KEY: {"mail": "a@b.de"}})

    assert await enaio_headers.get_enaio_headers(ctx) == {"mail": "a@b.de"}


async def test_get_enaio_headers_with_synchronous_state_api():
    """Auch eine synchrone State-API (FastMCP 2) wird unterstuetzt."""

    ctx = _SyncStateContext({enaio_headers.ENAIO_STATE_KEY: {"mail": "a@b.de"}})

    assert await enaio_headers.get_enaio_headers(ctx) == {"mail": "a@b.de"}


async def test_get_enaio_headers_without_state():
    assert await enaio_headers.get_enaio_headers(_FastMCPContext()) == {}


async def test_get_enaio_headers_without_get_state():
    """Contexts ohne State-Unterstuetzung liefern ein leeres Dict statt eines Fehlers."""

    assert await enaio_headers.get_enaio_headers(SimpleNamespace()) == {}
