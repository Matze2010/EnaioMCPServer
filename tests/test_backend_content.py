"""Tests fuer das Auslesen von MIME-Type und Dateiname aus Inhaltsantworten."""

import httpx

from EnaioBackend import UPLOAD_OBJECT_TYPE_ID


def _file_handler(headers):
    """MockTransport-Handler, der die Datei mit ``headers`` ausliefert."""

    def handler(request):
        assert request.url.path == "/api/dms/objects/OBJ1/contents/file/1"
        return httpx.Response(200, content=b"BINARY", headers=headers)

    return handler


async def test_get_file_reads_mime_type_and_filename(make_backend):
    backend = make_backend(
        _file_handler(
            {
                "Content-Type": "application/pdf; charset=binary",
                "Content-Disposition": 'attachment; filename="Rechnung 2024.pdf"',
            }
        )
    )

    content, mime_type, filename = await backend.get_file("OBJ1")

    assert content == b"BINARY"
    # Die Parameter hinter dem Semikolon gehoeren nicht zum MIME-Type.
    assert mime_type == "application/pdf"
    assert filename == "Rechnung 2024.pdf"


async def test_get_file_reads_rfc2231_filename(make_backend):
    backend = make_backend(
        _file_handler(
            {
                "Content-Type": "image/png",
                "Content-Disposition": "attachment; filename*=UTF-8''Pr%C3%BCfbericht.png",
            }
        )
    )

    _, mime_type, filename = await backend.get_file("OBJ1")

    assert mime_type == "image/png"
    assert filename == "Prüfbericht.png"


async def test_get_file_without_headers_falls_back(make_backend):
    # httpx setzt ohne Angabe keinen Content-Type auf der Antwort.
    backend = make_backend(_file_handler({}))

    content, mime_type, filename = await backend.get_file("OBJ1")

    assert content == b"BINARY"
    assert mime_type == "application/octet-stream"
    assert filename is None


async def test_get_document_passes_file_info_through(make_backend):
    def handler(request):
        if request.url.path == "/api/dms/objects/search":
            return httpx.Response(
                200,
                json={
                    "objects": [
                        {
                            "properties": {
                                "system:objectTypeId": {"value": UPLOAD_OBJECT_TYPE_ID},
                                "system:objectId": {"value": "OBJ1"},
                                "AA_DOK_PENR": {"value": "2024-42"},
                                "Betreff": {"value": "Ein Dokument"},
                                "system:creationDate": {"value": "2024-01-01"},
                                "system:lastModificationDate": {"value": "2024-01-02"},
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            content=b"BINARY",
            headers={
                "Content-Type": "application/pdf",
                "Content-Disposition": 'inline; filename="Bescheid.pdf"',
            },
        )

    backend = make_backend(handler)
    document = await backend.get_document("2024-42", "file")

    assert document["content"] == b"BINARY"
    assert document["mime_type"] == "application/pdf"
    assert document["filename"] == "Bescheid.pdf"


async def test_get_document_rendition_is_marked_as_text(make_backend):
    def handler(request):
        if request.url.path == "/api/dms/objects/search":
            return httpx.Response(
                200,
                json={
                    "objects": [
                        {
                            "properties": {
                                "system:objectTypeId": {"value": UPLOAD_OBJECT_TYPE_ID},
                                "system:objectId": {"value": "OBJ1"},
                                "AA_DOK_PENR": {"value": "2024-42"},
                                "Betreff": {"value": "Ein Dokument"},
                                "system:creationDate": {"value": "2024-01-01"},
                                "system:lastModificationDate": {"value": "2024-01-02"},
                            }
                        }
                    ]
                },
            )
        return httpx.Response(200, text="Volltext")

    backend = make_backend(handler)
    document = await backend.get_document("2024-42", "text")

    assert document["mime_type"] == "text/plain"
    assert document["filename"] is None
