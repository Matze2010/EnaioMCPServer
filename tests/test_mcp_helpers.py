"""Tests fuer die aus den MCP-Tools ausgelagerten Hilfsfunktionen."""

import re
from datetime import datetime

import pytest
from fastapi import HTTPException

import EnaioMCP
from middleware import ENAIO_STATE_KEY


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Vermerk", "Vermerk"),
        ("Mein Betreff", "Mein_Betreff"),
        ("Grüße/Umlaute!", "Gr_e_Umlaute"),
        ("  ", "dokument"),
        (None, "dokument"),
        ("...", "dokument"),
    ],
)
def test_sanitize_filename(raw, expected):
    assert EnaioMCP._sanitize_filename(raw) == expected


def test_resolve_template_unknown_type_raises_400():
    with pytest.raises(HTTPException) as excinfo:
        EnaioMCP._resolve_template("Gutachten")

    assert excinfo.value.status_code == 400
    # Die verfuegbaren Typen werden zur Orientierung mitgegeben.
    assert "vermerk" in excinfo.value.detail


def test_resolve_template_missing_file_raises_404(monkeypatch, tmp_path):
    monkeypatch.setattr(EnaioMCP, "ASSETS_DIR", tmp_path)

    with pytest.raises(HTTPException) as excinfo:
        EnaioMCP._resolve_template("Vermerk")

    assert excinfo.value.status_code == 404
    assert "Vorlage_Vermerk.docx" in excinfo.value.detail


def test_resolve_template_is_case_insensitive(monkeypatch, tmp_path):
    monkeypatch.setattr(EnaioMCP, "ASSETS_DIR", tmp_path)
    (tmp_path / "Vorlage_Brief.docx").write_bytes(b"PK")

    name, path = EnaioMCP._resolve_template("  BRIEF ")

    assert name == "Vorlage_Brief.docx"
    assert path == tmp_path / "Vorlage_Brief.docx"


def test_output_path_uses_timestamp_type_and_betreff(monkeypatch, tmp_path):
    monkeypatch.setattr(EnaioMCP, "OUTPUT_DIR", tmp_path)

    path, name = EnaioMCP._output_path("Vermerk", "Mein Betreff")

    assert path == tmp_path / name
    assert name.endswith("_Vermerk_Mein_Betreff.docx")
    # Praefix ist ein Zeitstempel der Form YYYYMMDD-HHMMSS.
    timestamp = name.split("_")[0]
    assert len(timestamp) == 15 and timestamp[8] == "-"
    assert timestamp.replace("-", "").isdigit()


def test_output_path_without_betreff_has_no_suffix(monkeypatch, tmp_path):
    monkeypatch.setattr(EnaioMCP, "OUTPUT_DIR", tmp_path)

    _, name = EnaioMCP._output_path("Brief", None)

    assert name.endswith("_Brief.docx")


def test_render_document_maps_template_errors_to_422(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise ValueError("Kein <w:sectPr> gefunden")

    monkeypatch.setattr(EnaioMCP.vorlage, "fill_document", boom)

    with pytest.raises(HTTPException) as excinfo:
        EnaioMCP._render_document(tmp_path / "t.docx", [], tmp_path / "o.docx", None, {})

    assert excinfo.value.status_code == 422
    assert "Kein <w:sectPr> gefunden" in excinfo.value.detail


class _Ctx:
    """Context-Ersatz, der die von der Middleware abgelegten Header liefert."""

    def __init__(self, enaio=None):
        self._state = {ENAIO_STATE_KEY: enaio} if enaio is not None else {}

    async def get_state(self, key):
        return self._state.get(key)


async def test_document_fields_uses_reference_and_headers():
    fields = await EnaioMCP._document_fields(
        "DS.1.2-2024-1234",
        None,
        _Ctx({"mail": "mathias.gisch@me.com", "name": "admin-gisch"}),
    )

    assert fields == {
        "Aktenzeichen": "DS.1.2-2024-1234",
        "Mail": "mathias.gisch@me.com",
        "Name": "admin-gisch",
    }


async def test_document_fields_keeps_explicit_values():
    """Ausdruecklich uebergebene Werte haben Vorrang vor Header und reference."""

    fields = await EnaioMCP._document_fields(
        "DS.1.2-2024-1234",
        {"Aktenzeichen": "DS.9.9-2020-1", "Name": "Max Mustermann"},
        _Ctx({"name": "admin-gisch", "mail": "a@b.de"}),
    )

    assert fields == {
        "Aktenzeichen": "DS.9.9-2020-1",
        "Name": "Max Mustermann",
        "Mail": "a@b.de",
    }


async def test_document_fields_without_headers():
    """Ohne HTTP-Request (z. B. stdio) bleibt es beim Aktenzeichen."""

    assert await EnaioMCP._document_fields("DS.1.2-2024-1234", None, _Ctx()) == {
        "Aktenzeichen": "DS.1.2-2024-1234"
    }


async def test_document_fields_does_not_mutate_input():
    original = {"Bearbeiter": "Max Mustermann"}

    await EnaioMCP._document_fields(
        "DS.1.2-2024-1234", original, _Ctx({"mail": "a@b.de"})
    )

    assert original == {"Bearbeiter": "Max Mustermann"}


def test_dms_link_uses_example_format(monkeypatch):
    monkeypatch.setattr(EnaioMCP, "DMS_WEB_URL", "https://enaio.test")

    link = EnaioMCP._dms_link("132887")

    match = re.fullmatch(
        r"https://enaio\.test/osweb/#/folder/132887/0"
        r"\?state=(\d+)&currentId=132887&currentTypeId=0",
        link,
    )
    assert match is not None
    # state ist ein Zeitstempel in Millisekunden und liegt nahe an "jetzt".
    state = int(match.group(1))
    assert abs(state - datetime.now().timestamp() * 1000) < 60_000


def test_dms_link_strips_trailing_slash(monkeypatch):
    monkeypatch.setattr(EnaioMCP, "DMS_WEB_URL", "https://enaio.test/")

    assert EnaioMCP._dms_link("42").startswith("https://enaio.test/osweb/#/folder/42/0?")


@pytest.mark.parametrize(
    "base, object_id",
    [
        ("https://enaio.test", None),
        ("https://enaio.test", ""),
        ("", "132887"),
        (None, "132887"),
        # Platzhalter aus dem Default, wenn keine URL konfiguriert ist.
        ("DEFAULT_URL", "132887"),
    ],
)
def test_dms_link_returns_none_without_usable_input(monkeypatch, base, object_id):
    monkeypatch.setattr(EnaioMCP, "DMS_WEB_URL", base)

    assert EnaioMCP._dms_link(object_id) is None


async def test_enforce_upload_rate_limit_maps_to_429(monkeypatch):
    from rate_limiter import RateLimiter

    monkeypatch.setattr(EnaioMCP, "upload_limiter", RateLimiter(1))

    await EnaioMCP._enforce_upload_rate_limit()  # erster Slot ist frei

    with pytest.raises(HTTPException) as excinfo:
        await EnaioMCP._enforce_upload_rate_limit()

    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"].isdigit()
