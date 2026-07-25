"""Tests fuer die aus den MCP-Tools ausgelagerten Hilfsfunktionen."""

import pytest
from fastapi import HTTPException

import EnaioMCP


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


async def test_enforce_upload_rate_limit_maps_to_429(monkeypatch):
    from rate_limiter import RateLimiter

    monkeypatch.setattr(EnaioMCP, "upload_limiter", RateLimiter(1))

    await EnaioMCP._enforce_upload_rate_limit()  # erster Slot ist frei

    with pytest.raises(HTTPException) as excinfo:
        await EnaioMCP._enforce_upload_rate_limit()

    assert excinfo.value.status_code == 429
    assert excinfo.value.headers["Retry-After"].isdigit()
