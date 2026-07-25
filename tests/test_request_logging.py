"""Tests fuer das Header-Logging bei Tool-Aufrufen."""

import logging
from types import SimpleNamespace

import pytest

import logging_config
import request_logging


def _context(tool_name="get_case_metadata"):
    """Minimaler Ersatz fuer den MiddlewareContext (nur ``message.name`` wird genutzt)."""
    return SimpleNamespace(message=SimpleNamespace(name=tool_name))


def test_format_headers_sorted_and_plaintext():
    formatted = request_logging.format_headers(
        {
            "user-agent": "claude-desktop/1.4",
            "authorization": "Bearer test123",
            "accept": "application/json",
        }
    )

    # Sortiert nach Header-Namen, Werte bewusst im Klartext.
    assert formatted == (
        "accept=application/json, authorization=Bearer test123, "
        "user-agent=claude-desktop/1.4"
    )


def test_format_headers_without_headers():
    assert request_logging.format_headers({}) == request_logging.NO_HTTP_REQUEST


async def test_on_call_tool_logs_headers_and_passes_through(monkeypatch, caplog):
    sentinel = object()
    seen = []

    async def call_next(context):
        seen.append(context)
        return sentinel

    monkeypatch.setattr(
        request_logging,
        "get_http_headers",
        lambda include_all=False: {"authorization": "Bearer test123", "x-test": "abc"},
    )

    context = _context()
    middleware = request_logging.RequestHeaderLoggingMiddleware()
    with caplog.at_level(logging.INFO, logger="EnaioMCP"):
        result = await middleware.on_call_tool(context, call_next)

    # Der Aufruf wird unveraendert weitergereicht.
    assert result is sentinel
    assert seen == [context]

    records = [r for r in caplog.records if r.name == "EnaioMCP"]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    assert records[0].getMessage() == (
        "Tool-Aufruf get_case_metadata - HTTP-Headers: "
        "authorization=Bearer test123, x-test=abc"
    )


async def test_on_call_tool_requests_all_headers(monkeypatch):
    """Ohne ``include_all`` wuerden u. a. host und authorization fehlen."""

    calls = []

    async def call_next(context):
        return None

    def fake_get_http_headers(include_all=False):
        calls.append(include_all)
        return {}

    monkeypatch.setattr(request_logging, "get_http_headers", fake_get_http_headers)

    await request_logging.RequestHeaderLoggingMiddleware().on_call_tool(
        _context(), call_next
    )

    assert calls == [True]


async def test_on_call_tool_without_http_request(monkeypatch, caplog):
    """Beim stdio-Transport gibt es keine Header - der Aufruf laeuft trotzdem durch."""

    async def call_next(context):
        return "ok"

    monkeypatch.setattr(
        request_logging, "get_http_headers", lambda include_all=False: {}
    )

    middleware = request_logging.RequestHeaderLoggingMiddleware()
    with caplog.at_level(logging.INFO, logger="EnaioMCP"):
        result = await middleware.on_call_tool(_context("download_document"), call_next)

    assert result == "ok"
    assert (
        f"Tool-Aufruf download_document - HTTP-Headers: {request_logging.NO_HTTP_REQUEST}"
        in caplog.text
    )


@pytest.mark.parametrize("logger_name", logging_config.APP_LOGGERS)
def test_configure_logging_applies_level_to_app_loggers(monkeypatch, logger_name):
    """Das LOG_LEVEL muss auch auf dem Logger des Header-Loggings ankommen."""

    logger = logging.getLogger(logger_name)
    original_level = logger.level
    try:
        monkeypatch.setenv("LOG_LEVEL", "DEBUG")
        assert logging_config.configure_logging() == logging.DEBUG
        assert logger.level == logging.DEBUG
    finally:
        logger.setLevel(original_level)
