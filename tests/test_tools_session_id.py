"""Tests fuer die AuthMode-abhaengigen Tool- und Resource-Schemas."""

import importlib
from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ToolError

import EnaioMCP
from middleware import (
    SESSION_ID_DESCRIPTION,
    SESSION_ID_REQUIRED_MESSAGE,
    EnaioSessionIDMiddleware,
)


def _context(arguments=None, tool_name="get_case_metadata"):
    """Minimaler Middleware-Context mit Tool-Name und Argumenten."""

    return SimpleNamespace(message=SimpleNamespace(name=tool_name, arguments=arguments))


@pytest.fixture
def load_enaio_mcp(monkeypatch):
    """Laedt EnaioMCP mit einem bestimmten AUTH_MODE und stellt session wieder her."""

    def _load(auth_mode):
        monkeypatch.setenv("AUTH_MODE", auth_mode)
        return importlib.reload(EnaioMCP)

    yield _load

    monkeypatch.setenv("AUTH_MODE", "session")
    importlib.reload(EnaioMCP)


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"SessionID": None},
        {"SessionID": ""},
        {"SessionID": "   "},
    ],
)
async def test_session_id_middleware_rejects_missing_or_empty_value(arguments):
    async def call_next(_context):
        raise AssertionError("Tool-Aufruf darf ohne SessionID nicht weiterlaufen")

    middleware = EnaioSessionIDMiddleware()

    with pytest.raises(ToolError) as excinfo:
        await middleware.on_call_tool(_context(arguments), call_next)

    assert str(excinfo.value) == SESSION_ID_REQUIRED_MESSAGE


async def test_session_id_middleware_passes_non_empty_value():
    seen = []

    async def call_next(context):
        seen.append(context)
        return "ok"

    context = _context({"SessionID": "SESSION-1"})
    middleware = EnaioSessionIDMiddleware()

    assert await middleware.on_call_tool(context, call_next) == "ok"
    assert seen == [context]


@pytest.mark.parametrize(
    "tool_name",
    [
        "get_case_metadata",
        "list_running_cases",
        "create_case_document",
        "access_document_fulltext",
        "download_document",
    ],
)
async def test_session_id_is_required_in_all_tool_schemas(tool_name):
    tool = await EnaioMCP.mcp.get_tool(tool_name)

    assert "SessionID" in tool.parameters["required"]
    assert tool.parameters["properties"]["SessionID"]["description"] == SESSION_ID_DESCRIPTION


async def test_resources_are_hidden_in_session_mode():
    templates = await EnaioMCP.mcp.list_resource_templates()

    assert templates == []


@pytest.mark.parametrize(
    "tool_name",
    [
        "get_case_metadata",
        "list_running_cases",
        "create_case_document",
        "access_document_fulltext",
        "download_document",
    ],
)
async def test_basic_tool_schemas_do_not_include_session_id(load_enaio_mcp, tool_name):
    module = load_enaio_mcp("basic")
    tool = await module.mcp.get_tool(tool_name)

    assert tool.version == "basic"
    assert "SessionID" not in tool.parameters.get("required", [])
    assert "SessionID" not in tool.parameters["properties"]


async def test_basic_resource_templates_do_not_include_session_id(load_enaio_mcp):
    module = load_enaio_mcp("basic")
    templates = await module.mcp.list_resource_templates()

    assert [item.uri_template for item in templates] == [
        "document://{document}/fulltext",
        "document://{document}/file",
    ]
    for template in templates:
        assert template.parameters["required"] == ["document"]
        assert "SessionID" not in template.parameters["properties"]


async def test_basic_tool_passes_no_session_id_to_backend(load_enaio_mcp, monkeypatch):
    module = load_enaio_mcp("basic")
    seen = []

    class Ctx:
        async def info(self, _message):
            pass

    async def fake_get_document(document_id, content_format, session_id=None):
        seen.append((document_id, content_format, session_id))
        return {"content": "Volltext"}

    monkeypatch.setattr(module.backend, "get_document", fake_get_document)

    result = await module.access_document_fulltext_basic("DOC-1", Ctx())

    assert result == "Volltext"
    assert seen == [("DOC-1", "text", None)]
