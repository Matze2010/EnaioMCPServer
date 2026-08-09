"""Tests fuer die Durchreichung der SessionID aus MCP-Tools ans Backend."""

import pytest
from fastapi import HTTPException

import EnaioMCP


class _Ctx:
    """Minimaler Ersatz fuer den FastMCP-Context (nur ``info`` wird genutzt)."""

    def __init__(self):
        self.messages = []

    async def info(self, message):
        self.messages.append(message)


async def test_get_case_metadata_passes_session_id_to_backend(monkeypatch):
    seen = []

    async def fake_get_aktenzeichen(reference, session_id=None):
        seen.append(("get_aktenzeichen", reference, session_id))
        return "PARENT123", {
            "reference_nr": reference,
            "title": "Titel",
            "category": "Kategorie",
            "topics": ["A"],
            "sachbearbeiter": "gisch",
        }

    async def fake_get_document_list(parent_object_id, session_id=None):
        seen.append(("get_document_list", parent_object_id, session_id))
        return []

    monkeypatch.setattr(EnaioMCP.backend, "get_aktenzeichen", fake_get_aktenzeichen)
    monkeypatch.setattr(EnaioMCP.backend, "get_document_list", fake_get_document_list)

    await EnaioMCP.get_case_metadata_session("DS.1.2-2024-1234", "SESSION-1", _Ctx())

    assert seen == [
        ("get_aktenzeichen", "DS.1.2-2024-1234", "SESSION-1"),
        ("get_document_list", "PARENT123", "SESSION-1"),
    ]


async def test_access_document_fulltext_passes_session_id_to_backend(monkeypatch):
    seen = []

    async def fake_get_document(document_id, content_format, session_id=None):
        seen.append((document_id, content_format, session_id))
        return {"content": "Volltext"}

    monkeypatch.setattr(EnaioMCP.backend, "get_document", fake_get_document)

    result = await EnaioMCP.access_document_fulltext_session("DOC-1", "SESSION-1", _Ctx())

    assert result == "Volltext"
    assert seen == [("DOC-1", "text", "SESSION-1")]


def _document(**overrides):
    """Datensatz, wie ihn ``backend.get_document`` fuer eine Datei liefert."""

    document = {
        "type": "file",
        "document_nr": "DOC-1",
        "name": "Ein Dokument",
        "content": b"BINARY",
        "mime_type": "application/pdf",
        "filename": "Rechnung 2024.pdf",
    }
    document.update(overrides)
    return document


def _patch_get_document(monkeypatch, document, seen=None):
    async def fake_get_document(document_id, content_format, session_id=None):
        if seen is not None:
            seen.append((document_id, content_format, session_id))
        return document

    monkeypatch.setattr(EnaioMCP.backend, "get_document", fake_get_document)


async def test_download_document_passes_session_id_to_backend(monkeypatch):
    seen = []
    _patch_get_document(monkeypatch, _document(), seen)

    await EnaioMCP.download_document_session("DOC-1", "SESSION-1", _Ctx())

    assert seen == [("DOC-1", "file", "SESSION-1")]


async def test_download_document_returns_file_as_embedded_resource(monkeypatch):
    _patch_get_document(monkeypatch, _document())

    summary, embedded = await EnaioMCP.download_document_session("DOC-1", "SESSION-1", _Ctx())

    # Der Binaerinhalt steckt ausschliesslich in der Resource, nicht im Text -
    # nur so bleibt er aus dem Kontext des Modells heraus.
    assert summary.type == "text"
    assert "Ein Dokument" in summary.text
    assert "QklOQVJZ" not in summary.text

    assert embedded.type == "resource"
    assert embedded.resource.blob == "QklOQVJZ"
    assert embedded.resource.mimeType == "application/pdf"
    assert str(embedded.resource.uri) == "file:///Rechnung_2024.pdf"


async def test_download_document_falls_back_to_generic_mime_type(monkeypatch):
    _patch_get_document(monkeypatch, _document(mime_type=None, filename=None))

    _, embedded = await EnaioMCP.download_document_session("DOC-1", "SESSION-1", _Ctx())

    assert embedded.resource.mimeType == "application/octet-stream"
    # Ohne Content-Disposition wird der Betreff zum Dateinamen.
    assert str(embedded.resource.uri).startswith("file:///Ein_Dokument")


async def test_download_document_returns_vermerk_as_text_resource(monkeypatch):
    _patch_get_document(
        monkeypatch,
        _document(
            type="vermerk",
            content="Inhalt des Vermerks",
            mime_type="text/plain",
            filename=None,
        ),
    )

    _, embedded = await EnaioMCP.download_document_session("DOC-1", "SESSION-1", _Ctx())

    # Ein Vermerk hat keine Datei; sein Klartext gehoert in eine Text-Resource.
    assert embedded.resource.text == "Inhalt des Vermerks"
    assert embedded.resource.mimeType == "text/plain"
    assert not hasattr(embedded.resource, "blob")


async def test_download_document_without_content_raises_not_found(monkeypatch):
    _patch_get_document(monkeypatch, _document(content=None))

    with pytest.raises(HTTPException) as error:
        await EnaioMCP.download_document_session("DOC-1", "SESSION-1", _Ctx())

    assert error.value.status_code == 404


async def test_resource_fulltext_uses_basic_auth_without_session_id(monkeypatch):
    seen = []

    async def fake_get_document(document_id, content_format, session_id=None):
        seen.append((document_id, content_format, session_id))
        return {"content": "Volltext"}

    monkeypatch.setattr(EnaioMCP.backend, "get_document", fake_get_document)

    result = await EnaioMCP.resource_access_document_fulltext("DOC-1", _Ctx())

    assert result == "Volltext"
    assert seen == [("DOC-1", "text", None)]
