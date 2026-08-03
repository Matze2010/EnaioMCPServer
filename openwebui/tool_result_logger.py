"""
title: Tool Result Logger
author: Enaio MCP Server
author_url: https://github.com/Matze2010/EnaioMCPServer
version: 0.1.0
required_open_webui_version: 0.5.0
license: MIT
description: Patcht process_tool_result in der Open-WebUI-Middleware und loggt die Aufrufparameter als WARNING. Ueber die Valve "tools" wird festgelegt, fuer welche Tools geloggt wird.

Diese Function haengt sich per Monkey-Patch in
``open_webui.utils.middleware.process_tool_result`` ein - die Funktion, die
Open WebUI nach jedem Tool-Aufruf zum Aufbereiten des Ergebnisses ruft. Der
Patch umschliesst die Originalfunktion, schreibt deren Aufrufparameter auf
Level ``WARNING`` ins Log und delegiert danach unveraendert weiter. Am Verhalten
von Open WebUI aendert sich nichts.

``WARNING`` liegt ueber dem Open-WebUI-Default ``INFO``; die Zeilen sind also
ohne Anpassung des globalen Log-Levels sichtbar.

Die Datei ist bewusst isoliert: keine Importe aus dem Enaio-MCP-Server, nur
Standardbibliothek plus ``pydantic`` (in Open WebUI ohnehin vorhanden). Sie
laesst sich auch ohne installiertes Open WebUI importieren - der Patch wird dann
lediglich mit einer Fehlermeldung uebersprungen.
"""

import fnmatch
import functools
import inspect
import logging
import sys

from pydantic import BaseModel, Field

# Feste Kennung am Anfang jeder Log-Zeile, damit sich die Ausgabe in einem
# Open-WebUI-Log zuverlaessig greppen laesst.
LOG_PREFIX = "[tool_result_logger]"

# Modul, in dem die zu patchende Funktion lebt.
TARGET_MODULE = "open_webui.utils.middleware"

# Kandidaten fuer den Funktionsnamen in der Reihenfolge der Pruefung. Upstream
# heisst die Funktion aktuell ``process_tool_result`` (Singular); die
# Pluralform wird zuerst geprueft, damit eine kuenftige Umbenennung ohne
# Anpassung dieser Datei funktioniert.
TARGET_CANDIDATES = ("process_tool_results", "process_tool_result")

# Attribut, mit dem ein bereits installierter Wrapper markiert wird.
PATCH_MARKER = "__enaio_tool_result_logger__"

# Platzhalter fuer redigierte Werte.
REDACTED = "***"

# Aktuelle ``Filter``-Instanz. Der Wrapper liest daraus bei jedem Aufruf die
# Valves, damit Aenderungen aus der Open-WebUI-Oberflaeche sofort wirken
# (Open WebUI ersetzt ``instance.valves`` vor jedem Filter-Lauf).
_ACTIVE_FILTER = None

# Zustand des Patches fuer Statusmeldungen und Tests.
_PATCH_STATE: dict[str, str | None] = {"target": None, "error": None}


def _logger(name: str = "") -> logging.Logger:
    """Liefert den Logger fuer die Ausgabe (Default aus den Valves)."""

    return logging.getLogger(name or Filter.Valves().logger_name)


def _valves() -> "Filter.Valves":
    """Liefert die aktuell gueltigen Valves, notfalls die Defaults."""

    valves = getattr(_ACTIVE_FILTER, "valves", None)
    return valves if isinstance(valves, BaseModel) else Filter.Valves()


def _split_list(raw: str) -> list[str]:
    """Zerlegt eine komma-separierte Valve in eine Liste ohne Leereintraege."""

    return [item.strip() for item in (raw or "").split(",") if item.strip()]


def _tool_matches(tool_name: str | None, patterns: str) -> bool:
    """Prueft, ob fuer ``tool_name`` geloggt werden soll.

    ``patterns`` ist die komma-separierte ``tools``-Valve. ``*`` trifft alles;
    ansonsten wird jeder Eintrag als Glob-Muster (``get_*``, ``*case*``) gegen
    den klein geschriebenen Tool-Namen geprueft.

    :param tool_name: Name des Tools aus dem Aufruf, ggf. ``None``.
    :param patterns: Rohwert der ``tools``-Valve.
    """

    entries = _split_list(patterns)
    if not entries:
        return False
    name = (tool_name or "").strip().lower()
    return any(fnmatch.fnmatchcase(name, entry.lower()) for entry in entries)


def _redact(value, redact_keys: frozenset[str]):
    """Ersetzt in Dicts/Listen rekursiv die Werte sensibler Schluessel."""

    if isinstance(value, dict):
        return {
            key: (
                REDACTED
                if isinstance(key, str) and key.lower() in redact_keys
                else _redact(item, redact_keys)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, redact_keys) for item in value]
    return value


def _shorten(text: str, limit: int) -> str:
    """Kuerzt ``text`` auf ``limit`` Zeichen (``limit <= 0`` = unbegrenzt)."""

    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}... (gekuerzt, {len(text)} Zeichen)"


def _describe_request(request) -> str:
    """Kurz-Repr des FastAPI-Requests; das Objekt selbst waere unbrauchbar lang."""

    method = getattr(request, "method", None)
    url = getattr(request, "url", None)
    if method is None and url is None:
        return repr(request)
    return f"<Request {method} {url}>"


def _format_arguments(bound: inspect.BoundArguments, valves: "Filter.Valves") -> list[str]:
    """Formatiert die gebundenen Aufrufparameter als ``name=wert``-Liste.

    Unbekannte bzw. in kuenftigen Open-WebUI-Versionen ergaenzte Parameter
    werden generisch mitgeloggt; nur fuer die bekannten gibt es Sonderbehandlung
    (Kurz-Repr, Redaktion) und eigene Schalter-Valves.
    """

    redact_keys = frozenset(entry.lower() for entry in _split_list(valves.redact_keys))
    skip_by_valve = {
        "request": valves.log_request,
        "tool_result": valves.log_tool_result,
        "metadata": valves.log_metadata,
        "user": valves.log_user,
    }

    parts = []
    for name, value in bound.arguments.items():
        if not skip_by_valve.get(name, True):
            continue

        if name == "request":
            text = _describe_request(value)
        elif name in ("metadata", "user"):
            text = repr(_redact(value, redact_keys))
        else:
            text = repr(value)

        parts.append(f"{name}={_shorten(text, valves.max_value_length)}")
    return parts


def _log_call(original, args: tuple, kwargs: dict) -> None:
    """Schreibt die Aufrufparameter von ``original`` als WARNING ins Log."""

    valves = _valves()
    if not valves.enabled:
        return

    # Ueber die Signatur binden, damit positionale Argumente korrekt benannt
    # werden - auch wenn eine Open-WebUI-Version Parameter ergaenzt.
    try:
        bound = inspect.signature(original).bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = bound.arguments
    except (TypeError, ValueError):
        # Signatur passt nicht - lieber roh loggen als gar nicht.
        bound = None
        arguments = dict(kwargs)

    tool_name = arguments.get("tool_function_name")
    if not _tool_matches(tool_name, valves.tools):
        return

    logger = _logger(valves.logger_name)
    if bound is None:
        logger.warning(
            "%s %s(args=%s, kwargs=%s)",
            LOG_PREFIX,
            getattr(original, "__name__", "process_tool_result"),
            _shorten(repr(args), valves.max_value_length),
            _shorten(repr(kwargs), valves.max_value_length),
        )
        return

    logger.warning(
        "%s %s(%s)",
        LOG_PREFIX,
        getattr(original, "__name__", "process_tool_result"),
        ", ".join(_format_arguments(bound, valves)),
    )


def _unwrap(function):
    """Liefert die Originalfunktion, falls ``function`` bereits ein Wrapper ist."""

    while getattr(function, PATCH_MARKER, False):
        wrapped = getattr(function, "__wrapped__", None)
        if wrapped is None:
            break
        function = wrapped
    return function


def _make_wrapper(original):
    """Erzeugt den loggenden Wrapper um ``original``."""

    @functools.wraps(original)
    async def wrapper(*args, **kwargs):
        try:
            _log_call(original, args, kwargs)
        except Exception:
            # Das Logging darf einen Tool-Aufruf niemals brechen.
            _logger().exception("%s Logging fehlgeschlagen", LOG_PREFIX)
        return await original(*args, **kwargs)

    setattr(wrapper, PATCH_MARKER, True)
    return wrapper


def _rebind_references(original, wrapper) -> None:
    """Biegt Direktreferenzen auf ``original`` in anderen Modulen mit um.

    Deckt ``from open_webui.utils.middleware import process_tool_result`` ab:
    ein ``setattr`` auf das Definitionsmodul allein wuerde solche bereits
    gebundenen Namen nicht erreichen.
    """

    for module_name, module in list(sys.modules.items()):
        if not module_name.startswith("open_webui") or module is None:
            continue
        for attribute, value in list(vars(module).items()):
            if value is original:
                setattr(module, attribute, wrapper)


def _install_patch(target_function: str = "") -> None:
    """Installiert den Monkey-Patch; idempotent und ohne Wrapper-Stapel.

    :param target_function: Expliziter Funktionsname; leer = Auto-Detect ueber
        ``TARGET_CANDIDATES``.
    """

    try:
        __import__(TARGET_MODULE)
        module = sys.modules[TARGET_MODULE]
    except Exception as error:
        _PATCH_STATE["target"] = None
        _PATCH_STATE["error"] = f"{TARGET_MODULE} nicht importierbar: {error}"
        return

    candidates = (target_function.strip(),) if target_function.strip() else TARGET_CANDIDATES
    for name in candidates:
        current = getattr(module, name, None)
        if not callable(current):
            continue

        # Beim Speichern einer Function fuehrt Open WebUI das Modul erneut aus.
        # Ohne das Auspacken wuerde sich pro Speichervorgang ein Wrapper mehr
        # aufstapeln und jede Zeile mehrfach im Log stehen.
        original = _unwrap(current)
        wrapper = _make_wrapper(original)
        setattr(module, name, wrapper)
        _rebind_references(original, wrapper)

        _PATCH_STATE["target"] = f"{TARGET_MODULE}.{name}"
        _PATCH_STATE["error"] = None
        return

    _PATCH_STATE["target"] = None
    _PATCH_STATE["error"] = (
        f"Keine der Funktionen {', '.join(candidates)} in {TARGET_MODULE} gefunden"
    )


def _ensure_patch(target_function: str = "") -> None:
    """Installiert den Patch und meldet das Ergebnis einmalig ins Log."""

    expected = (
        f"{TARGET_MODULE}.{target_function.strip()}" if target_function.strip() else None
    )
    if _PATCH_STATE["target"] and (expected is None or _PATCH_STATE["target"] == expected):
        return

    _install_patch(target_function)
    logger = _logger(_valves().logger_name)
    if _PATCH_STATE["error"]:
        logger.error("%s Patch nicht installiert: %s", LOG_PREFIX, _PATCH_STATE["error"])
    else:
        logger.warning("%s Patch aktiv auf %s", LOG_PREFIX, _PATCH_STATE["target"])


class Filter:
    """Open-WebUI-Filter, der ausschliesslich den Monkey-Patch traegt.

    Die Anfrage wird nicht veraendert. ``inlet`` dient nur dazu, dass Open WebUI
    das Modul laedt und die aktuellen Valve-Werte auf der Instanz bereitstellt.
    """

    class Valves(BaseModel):
        priority: int = Field(
            default=0,
            description="Reihenfolge unter mehreren Filtern (kleiner = frueher).",
        )
        enabled: bool = Field(
            default=True,
            description="Master-Schalter fuer das Logging der Aufrufparameter.",
        )
        tools: str = Field(
            default="*",
            description=(
                "Komma-separierte Tool-Namen, fuer die geloggt wird. '*' = alle. "
                "Glob-Muster erlaubt, z. B. 'get_case_metadata, list_*'."
            ),
        )
        log_tool_result: bool = Field(
            default=True,
            description="Parameter 'tool_result' mitloggen (enthaelt Fachdaten im Klartext).",
        )
        log_metadata: bool = Field(
            default=True,
            description="Parameter 'metadata' mitloggen (chat_id, message_id, session_id, ...).",
        )
        log_user: bool = Field(
            default=True,
            description="Parameter 'user' mitloggen.",
        )
        log_request: bool = Field(
            default=False,
            description="Parameter 'request' als Kurzform '<Request METHODE URL>' mitloggen.",
        )
        max_value_length: int = Field(
            default=2000,
            description="Maximale Zeichenzahl je geloggtem Wert; 0 = unbegrenzt.",
        )
        redact_keys: str = Field(
            default="token,api_key,password,authorization,secret,cookie",
            description="Schluessel in 'metadata'/'user', deren Werte durch *** ersetzt werden.",
        )
        logger_name: str = Field(
            default="open_webui.tool_result_logger",
            description="Name des Loggers, auf den die WARNING-Zeilen gehen.",
        )
        target_function: str = Field(
            default="",
            description=(
                "Zu patchende Funktion in open_webui.utils.middleware. "
                "Leer = automatisch (process_tool_results, sonst process_tool_result)."
            ),
        )

    def __init__(self):
        global _ACTIVE_FILTER

        self.valves = self.Valves()
        _ACTIVE_FILTER = self
        _ensure_patch(self.valves.target_function)

    def inlet(self, body: dict, __user__: dict | None = None) -> dict:
        """Uebernimmt die aktuellen Valves und gibt die Anfrage unveraendert zurueck."""

        global _ACTIVE_FILTER

        _ACTIVE_FILTER = self
        _ensure_patch(self.valves.target_function)
        return body


# Patch bereits beim Laden des Moduls setzen, damit er auch dann greift, wenn
# der Filter erst spaeter im Request-Zyklus aufgerufen wird.
_ensure_patch()
