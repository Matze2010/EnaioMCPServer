"""Tests fuer die Open-WebUI-Function ``openwebui/tool_result_logger.py``.

Die Function ist bewusst kein Paket-Modul, deshalb wird sie hier ueber
``importlib`` direkt aus der Datei geladen - jeweils gegen ein in ``sys.modules``
gefaelschtes ``open_webui.utils.middleware``, sodass kein installiertes
Open WebUI noetig ist.
"""

import importlib.util
import logging
import os
import sys
import types

import pytest

MODULE_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "openwebui", "tool_result_logger.py")
)

ORIGINAL_RESULT = ("ergebnis", ["datei"], ["embed"])


async def _process_tool_result(
    request,
    tool_function_name,
    tool_result,
    tool_type,
    direct_tool=False,
    metadata=None,
    user=None,
):
    """Stellvertreter fuer die Open-WebUI-Funktion mit identischer Signatur."""

    return ORIGINAL_RESULT


def _install_fake_open_webui(monkeypatch) -> types.ModuleType:
    """Registriert ein Fake-``open_webui.utils.middleware`` und liefert es zurueck."""

    package = types.ModuleType("open_webui")
    package.__path__ = []
    utils = types.ModuleType("open_webui.utils")
    utils.__path__ = []
    middleware = types.ModuleType("open_webui.utils.middleware")
    middleware.process_tool_result = _process_tool_result

    package.utils = utils
    utils.middleware = middleware

    monkeypatch.setitem(sys.modules, "open_webui", package)
    monkeypatch.setitem(sys.modules, "open_webui.utils", utils)
    monkeypatch.setitem(sys.modules, "open_webui.utils.middleware", middleware)
    return middleware


def _load_logger_module() -> types.ModuleType:
    """Laedt die Function frisch aus der Datei (kein sys.modules-Cache)."""

    spec = importlib.util.spec_from_file_location(
        "openwebui_tool_result_logger", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def patched(monkeypatch):
    """Fake-Open-WebUI plus frisch geladene Function mit aktivem Filter."""

    middleware = _install_fake_open_webui(monkeypatch)
    module = _load_logger_module()
    filter_instance = module.Filter()
    return module, middleware, filter_instance


def _tool_calls(middleware):
    """Ruft die gepatchte Funktion einmal mit Beispielargumenten auf."""

    return middleware.process_tool_result(
        object(),
        "get_case_metadata",
        '{"aktenzeichen": "DS.1.2-2024-1234"}',
        "mcp",
        False,
        {"chat_id": "c1", "token": "geheim"},
        {"id": "u1", "name": "Tester"},
    )


def test_patch_wird_installiert(patched):
    module, middleware, _ = patched

    assert module._PATCH_STATE["error"] is None
    assert module._PATCH_STATE["target"] == "open_webui.utils.middleware.process_tool_result"
    assert getattr(middleware.process_tool_result, module.PATCH_MARKER, False) is True


async def test_aufruf_loggt_parameter_als_warning_und_liefert_original(patched, caplog):
    module, middleware, _ = patched

    with caplog.at_level(logging.WARNING, logger="open_webui.tool_result_logger"):
        result = await _tool_calls(middleware)

    assert result == ORIGINAL_RESULT

    records = [r for r in caplog.records if module.LOG_PREFIX in r.getMessage()]
    assert len(records) == 1
    message = records[0].getMessage()
    assert records[0].levelno == logging.WARNING
    assert "process_tool_result(" in message
    assert "tool_function_name='get_case_metadata'" in message
    assert "tool_type='mcp'" in message
    assert "direct_tool=False" in message
    assert "DS.1.2-2024-1234" in message
    # request ist per Default abgeschaltet, sensible Schluessel sind redigiert.
    assert "request=" not in message
    assert "geheim" not in message
    assert module.REDACTED in message


async def test_tools_valve_filtert_fremde_tools(patched, caplog):
    module, middleware, filter_instance = patched
    filter_instance.valves.tools = "list_inbox"

    with caplog.at_level(logging.WARNING, logger="open_webui.tool_result_logger"):
        await _tool_calls(middleware)

    assert [r for r in caplog.records if module.LOG_PREFIX in r.getMessage()] == []


@pytest.mark.parametrize("pattern", ["*", "get_case_metadata", "get_*", " list_inbox , get_case_metadata "])
async def test_tools_valve_trifft_passende_muster(patched, caplog, pattern):
    module, middleware, filter_instance = patched
    filter_instance.valves.tools = pattern

    with caplog.at_level(logging.WARNING, logger="open_webui.tool_result_logger"):
        await _tool_calls(middleware)

    assert len([r for r in caplog.records if module.LOG_PREFIX in r.getMessage()]) == 1


async def test_enabled_false_schaltet_logging_ab(patched, caplog):
    module, middleware, filter_instance = patched
    filter_instance.valves.enabled = False

    with caplog.at_level(logging.WARNING, logger="open_webui.tool_result_logger"):
        await _tool_calls(middleware)

    assert [r for r in caplog.records if module.LOG_PREFIX in r.getMessage()] == []


async def test_mehrfaches_patchen_stapelt_keine_wrapper(patched, caplog):
    module, middleware, _ = patched

    # Entspricht dem erneuten Speichern der Function in Open WebUI.
    module._PATCH_STATE["target"] = None
    module._ensure_patch()

    with caplog.at_level(logging.WARNING, logger="open_webui.tool_result_logger"):
        result = await _tool_calls(middleware)

    assert result == ORIGINAL_RESULT
    calls = [
        r
        for r in caplog.records
        if "process_tool_result(" in r.getMessage() and module.LOG_PREFIX in r.getMessage()
    ]
    assert len(calls) == 1


async def test_direktreferenz_in_anderem_modul_wird_umgebogen(monkeypatch, caplog):
    middleware = _install_fake_open_webui(monkeypatch)

    # Simuliert "from open_webui.utils.middleware import process_tool_result".
    consumer = types.ModuleType("open_webui.utils.chat")
    consumer.process_tool_result = middleware.process_tool_result
    monkeypatch.setitem(sys.modules, "open_webui.utils.chat", consumer)

    module = _load_logger_module()
    module.Filter()

    assert getattr(consumer.process_tool_result, module.PATCH_MARKER, False) is True

    with caplog.at_level(logging.WARNING, logger="open_webui.tool_result_logger"):
        result = await consumer.process_tool_result(
            object(), "get_case_metadata", "x", "mcp"
        )

    assert result == ORIGINAL_RESULT
    assert [r for r in caplog.records if module.LOG_PREFIX in r.getMessage()]


def test_max_value_length_kuerzt_lange_werte(patched, caplog):
    module, _, filter_instance = patched
    filter_instance.valves.max_value_length = 20

    text = module._shorten("x" * 100, 20)

    assert text.startswith("x" * 20)
    assert "gekuerzt, 100 Zeichen" in text
    assert module._shorten("x" * 100, 0) == "x" * 100


def test_import_ohne_open_webui_wirft_nicht(monkeypatch):
    for name in ("open_webui", "open_webui.utils", "open_webui.utils.middleware"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    # Import von open_webui sicher scheitern lassen, falls doch installiert.
    monkeypatch.setattr(sys, "path", [p for p in sys.path if "open_webui" not in p])
    monkeypatch.setitem(sys.modules, "open_webui", None)

    module = _load_logger_module()

    assert module._PATCH_STATE["target"] is None
    assert module._PATCH_STATE["error"]
