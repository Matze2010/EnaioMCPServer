"""Tests fuer die verpflichtende ``SessionID`` aller MCP-Tools und Resources."""

from types import SimpleNamespace

import pytest
from fastmcp.exceptions import ResourceError, ToolError

import EnaioMCP
from middleware import (
    SESSION_ID_DESCRIPTION,
    SESSION_ID_REQUIRED_MESSAGE,
    EnaioSessionIDMiddleware,
    has_usable_resource_session_id,
    session_id_from_resource_uri,
)


def _context(arguments=None, tool_name="get_case_metadata"):
    """Minimaler Middleware-Context mit Tool-Name und Argumenten."""

    return SimpleNamespace(message=SimpleNamespace(name=tool_name, arguments=arguments))


def _resource_context(uri="document://SESSION-1/132887/fulltext"):
    """Minimaler Middleware-Context mit Resource-URI."""

    return SimpleNamespace(message=SimpleNamespace(uri=uri))


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


@pytest.mark.parametrize(
    "uri",
    [
        None,
        "",
        "document:///132887/fulltext",
        "document://   /132887/fulltext",
    ],
)
async def test_session_id_middleware_rejects_missing_or_empty_resource_session(uri):
    async def call_next(_context):
        raise AssertionError("Resource-Aufruf darf ohne SessionID nicht weiterlaufen")

    middleware = EnaioSessionIDMiddleware()

    with pytest.raises(ResourceError) as excinfo:
        await middleware.on_read_resource(_resource_context(uri), call_next)

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


async def test_session_id_middleware_passes_non_empty_resource_session():
    seen = []

    async def call_next(context):
        seen.append(context)
        return "ok"

    context = _resource_context("document://SESSION-1/132887/fulltext")
    middleware = EnaioSessionIDMiddleware()

    assert await middleware.on_read_resource(context, call_next) == "ok"
    assert seen == [context]


def test_resource_session_id_helpers_parse_document_uri():
    assert session_id_from_resource_uri("document://SESSION-1/132887/fulltext") == "SESSION-1"
    assert has_usable_resource_session_id("document://SESSION-1/132887/file") is True
    assert has_usable_resource_session_id("document:///132887/file") is False


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


@pytest.mark.parametrize(
    "uri_template",
    [
        "document://{SessionID}/{document}/fulltext",
        "document://{SessionID}/{document}/file",
    ],
)
async def test_session_id_is_required_in_all_resource_templates(uri_template):
    templates = await EnaioMCP.mcp.list_resource_templates()
    template = next(item for item in templates if item.uri_template == uri_template)

    assert "SessionID" in template.parameters["required"]
    assert "document" in template.parameters["required"]
