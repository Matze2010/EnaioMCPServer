"""Tests fuer die Durchreichung der SessionID aus MCP-Tools ans Backend."""

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

    await EnaioMCP.get_case_metadata("DS.1.2-2024-1234", "SESSION-1", _Ctx())

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

    result = await EnaioMCP.access_document_fulltext("DOC-1", "SESSION-1", _Ctx())

    assert result == "Volltext"
    assert seen == [("DOC-1", "text", "SESSION-1")]


async def test_download_document_passes_session_id_to_backend(monkeypatch):
    seen = []

    async def fake_get_document(document_id, content_format, session_id=None):
        seen.append((document_id, content_format, session_id))
        return {"content": b"BINARY"}

    monkeypatch.setattr(EnaioMCP.backend, "get_document", fake_get_document)

    result = await EnaioMCP.download_document("DOC-1", "SESSION-1", _Ctx())

    assert result == "QklOQVJZ"
    assert seen == [("DOC-1", "file", "SESSION-1")]


async def test_resource_fulltext_passes_session_id_to_backend(monkeypatch):
    seen = []

    async def fake_get_document(document_id, content_format, session_id=None):
        seen.append((document_id, content_format, session_id))
        return {"content": "Volltext"}

    monkeypatch.setattr(EnaioMCP.backend, "get_document", fake_get_document)

    result = await EnaioMCP.resource_access_document_fulltext("SESSION-1", "DOC-1", _Ctx())

    assert result == "Volltext"
    assert seen == [("DOC-1", "text", "SESSION-1")]
