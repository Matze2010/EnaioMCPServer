"""Tests fuer den Client der Mistral-OCR-API."""

import base64
import json

import httpx
import pytest

from mistral_ocr import OCR_PATH, OCRUnavailable

PDF = b"%PDF-1.4 Scan"
PNG = b"\x89PNG\r\n\x1a\n"


def _ocr_handler(payload, status_code=200, calls=None):
    """MockTransport-Handler, der eine OCR-Antwort liefert und Requests sammelt."""

    def handler(request):
        if calls is not None:
            calls.append(request)
        return httpx.Response(status_code, json=payload)

    return handler


def _body(request):
    return json.loads(request.content)


async def test_extract_text_joins_page_markdown(make_ocr_client):
    client = make_ocr_client(
        _ocr_handler({"pages": [{"index": 0, "markdown": "Seite 1"},
                                {"index": 1, "markdown": "Seite 2"}]})
    )

    text = await client.extract_text(PDF, "application/pdf", context="Dokument 1")

    assert text == "Seite 1\n\nSeite 2"


async def test_extract_text_sends_document_url_for_pdf(make_ocr_client):
    calls = []
    client = make_ocr_client(
        _ocr_handler({"pages": [{"markdown": "Text"}]}, calls=calls),
        model="mistral-ocr-2503",
    )

    await client.extract_text(PDF, "application/pdf", context="Dokument 1")

    request = calls[0]
    assert request.url.path == OCR_PATH
    assert request.headers["authorization"] == "Bearer TEST-KEY"

    body = _body(request)
    assert body["model"] == "mistral-ocr-2503"
    assert body["document"]["type"] == "document_url"

    # Die Datei steckt als Base64-data-URI im document_url und muss
    # unveraendert zurueck-dekodierbar sein.
    prefix, encoded = body["document"]["document_url"].split(",", 1)
    assert prefix == "data:application/pdf;base64"
    assert base64.b64decode(encoded) == PDF


async def test_extract_text_sends_image_url_for_images(make_ocr_client):
    calls = []
    client = make_ocr_client(_ocr_handler({"pages": [{"markdown": "Text"}]}, calls=calls))

    await client.extract_text(PNG, "image/png", context="Dokument 1")

    document = _body(calls[0])["document"]

    # Mit dem Typ wechselt auch der Name des Wertfeldes.
    assert document["type"] == "image_url"
    assert "document_url" not in document
    assert document["image_url"].startswith("data:image/png;base64,")


async def test_extract_text_does_not_request_images(make_ocr_client):
    calls = []
    client = make_ocr_client(_ocr_handler({"pages": [{"markdown": "Text"}]}, calls=calls))

    await client.extract_text(PDF, "application/pdf", context="Dokument 1")

    assert _body(calls[0])["include_image_base64"] is False


async def test_extract_text_preserves_markdown_and_umlauts(make_ocr_client):
    markdown = "# Prüfbericht\n\n| Feld | Wert |\n| --- | --- |\n| Größe | 3 |"
    client = make_ocr_client(_ocr_handler({"pages": [{"markdown": markdown}]}))

    text = await client.extract_text(PDF, "application/pdf", context="Dokument 1")

    # Der Text wird bewusst nicht normiert (siehe stillgelegtes standardize_text).
    assert text == markdown


async def test_extract_text_rejects_unsupported_mime_type(make_ocr_client):
    calls = []
    client = make_ocr_client(_ocr_handler({"pages": [{"markdown": "Text"}]}, calls=calls))

    with pytest.raises(OCRUnavailable):
        await client.extract_text(b"MSG", "application/vnd.ms-outlook", context="Dokument 1")

    # Ein ungeeigneter Typ darf keinen Netzverkehr und keine Kosten verursachen.
    assert calls == []


async def test_extract_text_rejects_oversized_content(make_ocr_client):
    calls = []
    client = make_ocr_client(
        _ocr_handler({"pages": [{"markdown": "Text"}]}, calls=calls), max_bytes=4
    )

    with pytest.raises(OCRUnavailable):
        await client.extract_text(PDF, "application/pdf", context="Dokument 1")

    assert calls == []


async def test_extract_text_rejects_empty_content(make_ocr_client):
    calls = []
    client = make_ocr_client(_ocr_handler({"pages": []}, calls=calls))

    with pytest.raises(OCRUnavailable):
        await client.extract_text(b"", "application/pdf", context="Dokument 1")

    assert calls == []


async def test_extract_text_without_api_key_raises(make_ocr_client):
    calls = []
    client = make_ocr_client(_ocr_handler({"pages": []}, calls=calls), api_key="")

    with pytest.raises(OCRUnavailable):
        await client.extract_text(PDF, "application/pdf", context="Dokument 1")

    assert calls == []


async def test_extract_text_raises_on_error_status(make_ocr_client):
    client = make_ocr_client(_ocr_handler({"message": "rate limited"}, status_code=429))

    with pytest.raises(OCRUnavailable) as error:
        await client.extract_text(PDF, "application/pdf", context="Dokument 1")

    assert "429" in str(error.value)


async def test_extract_text_raises_on_request_error(make_ocr_client):
    def handler(request):
        raise httpx.ConnectError("kein Netz", request=request)

    client = make_ocr_client(handler)

    with pytest.raises(OCRUnavailable) as error:
        await client.extract_text(PDF, "application/pdf", context="Dokument 1")

    assert "nicht erreichbar" in str(error.value)


async def test_extract_text_raises_on_unreadable_payload(make_ocr_client):
    def handler(request):
        return httpx.Response(200, content=b"kein json")

    client = make_ocr_client(handler)

    with pytest.raises(OCRUnavailable):
        await client.extract_text(PDF, "application/pdf", context="Dokument 1")


@pytest.mark.parametrize(
    "payload",
    [{}, {"pages": []}, {"pages": [{"markdown": ""}, {"markdown": "   "}]}],
)
async def test_extract_text_raises_without_text(make_ocr_client, payload):
    # Ein als Erfolg gemeldeter Leertext wuerde den Rueckfall auf die
    # Enaio-Rendition stillschweigend verhindern.
    client = make_ocr_client(_ocr_handler(payload))

    with pytest.raises(OCRUnavailable):
        await client.extract_text(PDF, "application/pdf", context="Dokument 1")


def test_supports_follows_configured_mime_types(make_ocr_client):
    client = make_ocr_client(_ocr_handler({}), mime_types=("application/pdf",))

    assert client.supports("application/pdf")
    assert client.supports("APPLICATION/PDF")
    assert not client.supports("image/png")
    assert not client.supports(None)
