"""Tests fuer EnaioBackend.upload_document gegen einen gemockten httpx-Transport."""

import json

import httpx
import pytest
from fastapi import HTTPException


def _search_response():
    """Minimale Antwort fuer den Parent-Lookup (get_aktenzeichen)."""
    return {
        "objects": [
            {
                "properties": {
                    "system:objectId": {"value": "PARENT123"},
                    "system:creationDate": {"value": "2024-01-01"},
                    "Aktenzeichen": {"value": "DS.1.2-2024-1234"},
                    "Aktenbezeichnung": {"value": "Titel"},
                    "Kategorisierung": {"value": "Kategorie"},
                    "Aktenverantwortlicher": {"value": "Sachbearbeiter"},
                    "Aktenplaneintrag": {"value": "A|B"},
                    "Aktentyp": {"value": "Standardakte"},
                }
            }
        ]
    }


@pytest.fixture
def docx_file(tmp_path):
    path = tmp_path / "doc.docx"
    path.write_bytes(b"PKfake-docx-bytes")
    return path


async def test_upload_success_builds_expected_multipart(docx_file, make_backend):
    captured = {}

    def handler(request):
        if request.url.path == "/api/dms/objects/search":
            return httpx.Response(200, json=_search_response())
        if request.url.path == "/api/dms/objects":
            captured["url"] = str(request.url)
            captured["content_type"] = request.headers.get("content-type")
            captured["body"] = request.content
            return httpx.Response(
                200,
                json={
                    "objects": [
                        {
                            "properties": {
                                "system:objectId": {"value": "NEWDOC999"},
                                "system:objectTypeId": {"value": "262146"},
                            }
                        }
                    ]
                },
            )
        return httpx.Response(404)

    backend = make_backend(handler)
    result = await backend.upload_document(
        "DS.1.2-2024-1234", docx_file, "Vermerk", "Mein Betreff", "doc.docx"
    )

    assert result == {"objectId": "NEWDOC999", "reference_nr": "DS.1.2-2024-1234"}
    assert "minimalResponse=true" in captured["url"]

    content_type = captured["content_type"]
    assert content_type.startswith("multipart/form-data; boundary=")
    boundary = content_type.split("boundary=")[1]

    body = captured["body"].decode("latin-1")
    parts = body.split("--" + boundary)

    # data-Part finden und als JSON auswerten.
    data_part = next(p for p in parts if 'name="data"' in p)
    assert "application/json" in data_part
    json_str = data_part.split("\r\n\r\n", 1)[1].rstrip("\r\n")
    data = json.loads(json_str)

    props = data["objects"][0]["properties"]
    assert props["system:objectTypeId"]["value"] == "262146"
    assert props["system:parentId"]["value"] == "PARENT123"
    assert props["Betreff"]["value"] == "Mein Betreff"

    stream = data["objects"][0]["contentStreams"][0]
    assert stream["cid"] == "cid_document"
    assert stream["fileName"] == "doc.docx"

    # Inhalts-Part unter derselben cid + Binaerinhalt vorhanden.
    content_part = next(p for p in parts if 'name="cid_document"' in p)
    assert "Content-ID: cid_document" in content_part
    assert "PKfake-docx-bytes" in content_part

    # Abschlussgrenze vorhanden.
    assert body.rstrip("\r\n").endswith("--" + boundary + "--")


async def test_upload_betreff_fallback_to_filename(docx_file, make_backend):
    captured = {}

    def handler(request):
        if request.url.path == "/api/dms/objects/search":
            return httpx.Response(200, json=_search_response())
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={"objects": [{"properties": {"system:objectId": {"value": "X"}}}]},
        )

    backend = make_backend(handler)
    await backend.upload_document(
        "DS.1.2-2024-1234", docx_file, "Vermerk", None, "doc.docx"
    )

    body = captured["body"].decode("latin-1")
    assert '"Betreff": {"value": "doc.docx"}' in body


async def test_upload_rejected_with_422(docx_file, make_backend):
    def handler(request):
        if request.url.path == "/api/dms/objects/search":
            return httpx.Response(200, json=_search_response())
        return httpx.Response(422, text="Insert failed")

    backend = make_backend(handler)
    with pytest.raises(HTTPException) as excinfo:
        await backend.upload_document(
            "DS.1.2-2024-1234", docx_file, "Vermerk", "B", "doc.docx"
        )

    assert excinfo.value.status_code == 422
    assert "422" in excinfo.value.detail


async def test_upload_unexpected_status_maps_to_502(docx_file, make_backend):
    def handler(request):
        if request.url.path == "/api/dms/objects/search":
            return httpx.Response(200, json=_search_response())
        return httpx.Response(500, text="boom")

    backend = make_backend(handler)
    with pytest.raises(HTTPException) as excinfo:
        await backend.upload_document(
            "DS.1.2-2024-1234", docx_file, "Vermerk", "B", "doc.docx"
        )

    assert excinfo.value.status_code == 502
