"""Tests fuer die verpflichtende ``SessionID`` aller MCP-Tools."""

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
