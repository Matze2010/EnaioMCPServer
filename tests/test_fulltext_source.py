"""Tests der Volltextweiche im Backend: OCR, Rueckfall und Laengenbegrenzung."""

import httpx
import pytest
from fastapi import HTTPException

from EnaioBackend import TRUNCATION_MARKER, UPLOAD_OBJECT_TYPE_ID, truncate_text
from mistral_ocr import OCRUnavailable

# Objekttyp-ID eines Vermerks (OSTPL_AA_AN) - ein Vermerk hat keine Datei.
VERMERK_OBJECT_TYPE_ID = "262144"

FILE_PATH = "/api/dms/objects/OBJ1/contents/file/1"
RENDITION_PATH = "/api/dms/objects/OBJ1/contents/renditions/text"


class _StubOCR:
    """OCR-Client-Ersatz, der die uebergebene Datei aufzeichnet."""

    def __init__(self, text="# Prüfbericht\n\n| A | B |", error=None, mime_types=None):
        self.text = text
        self.error = error
        self.mime_types = mime_types or {"application/pdf"}
        self.calls = []

    def supports(self, mime_type):
        return (mime_type or "").lower() in self.mime_types

    async def extract_text(self, content, mime_type, *, context):
        # Wie im echten Client wird der MIME-Type geprueft, bevor irgendetwas
        # passiert - der Aufruf wird deshalb erst danach protokolliert.
        if not self.supports(mime_type):
            raise OCRUnavailable(f"MIME-Type {mime_type} ist nicht fuer die OCR vorgesehen")

        self.calls.append((content, mime_type, context))
        if self.error is not None:
            raise self.error
        return self.text


def _search_response(object_type_id=UPLOAD_OBJECT_TYPE_ID, notiz=None):
    properties = {
        "system:objectTypeId": {"value": object_type_id},
        "system:objectId": {"value": "OBJ1"},
        "AA_DOK_PENR": {"value": "2024-42"},
        "Betreff": {"value": "Ein Dokument"},
        "OSTPL_AA_AN_CONTACTMEDIA": {"value": "Telefon"},
        "system:creationDate": {"value": "2024-01-01"},
        "system:lastModificationDate": {"value": "2024-01-02"},
    }
    if notiz is not None:
        properties["OSTPL_AA_AN_NOTIZ"] = {"value": notiz}

    return httpx.Response(200, json={"objects": [{"properties": properties}]})


def _handler(paths, *, object_type_id=UPLOAD_OBJECT_TYPE_ID, notiz=None,
             file_response=None, rendition_text="Volltext aus Enaio"):
    """Handler fuer Suche, Originaldatei und Rendition; protokolliert die Pfade."""

    def handler(request):
        paths.append(request.url.path)

        if request.url.path == "/api/dms/objects/search":
            return _search_response(object_type_id, notiz)
        if request.url.path == FILE_PATH:
            return file_response if file_response is not None else httpx.Response(
                200, content=b"PDFBYTES", headers={"Content-Type": "application/pdf"}
            )
        return httpx.Response(200, text=rendition_text)

    return handler


# ----------------------------------------------------------------------
# truncate_text
# ----------------------------------------------------------------------


def test_truncate_text_leaves_short_text_untouched():
    assert truncate_text("kurz", 100) == "kurz"


@pytest.mark.parametrize("text", [None, ""])
def test_truncate_text_passes_empty_values_through(text):
    # None muss None bleiben, damit "kein Inhalt" erkennbar bleibt.
    assert truncate_text(text, 10) is text


@pytest.mark.parametrize("max_chars", [0, -1])
def test_truncate_text_disabled_by_non_positive_limit(max_chars):
    assert truncate_text("x" * 100, max_chars) == "x" * 100


def test_truncate_text_cuts_back_to_line_break():
    text = "Zeile eins\nZeile zwei\nZeile drei"

    result = truncate_text(text, 25)

    # Gekappt wird auf der letzten Zeilengrenze, nicht mitten in der Zeile.
    assert result == "Zeile eins\nZeile zwei" + TRUNCATION_MARKER


def test_truncate_text_cuts_hard_without_line_break():
    result = truncate_text("abcdefghij", 4)

    assert result == "abcd" + TRUNCATION_MARKER


# ----------------------------------------------------------------------
# get_rendition ohne Normierung
# ----------------------------------------------------------------------


async def test_get_rendition_preserves_case_umlauts_and_newlines(make_backend):
    original = "Prüfbericht Größe\nZweite Zeile"
    backend = make_backend(lambda request: httpx.Response(200, text=original))

    # Gegenprobe zur stillgelegten standardize_text: nichts wird normiert.
    assert await backend.get_rendition("OBJ1") == original


# ----------------------------------------------------------------------
# get_ocr_rendition
# ----------------------------------------------------------------------


async def test_get_ocr_rendition_returns_none_without_client(make_backend):
    paths = []
    backend = make_backend(_handler(paths))

    assert await backend.get_ocr_rendition("OBJ1") is None
    # Ohne OCR-Client darf die Originaldatei gar nicht erst geladen werden.
    assert paths == []


async def test_get_ocr_rendition_passes_file_and_mime_type(make_backend):
    ocr = _StubOCR()
    backend = make_backend(_handler([]), ocr_client=ocr)

    text = await backend.get_ocr_rendition("OBJ1")

    assert text == ocr.text
    content, mime_type, context = ocr.calls[0]
    assert content == b"PDFBYTES"
    assert mime_type == "application/pdf"
    assert "OBJ1" in context


async def test_get_ocr_rendition_returns_none_when_file_is_missing(make_backend):
    ocr = _StubOCR()
    backend = make_backend(
        _handler([], file_response=httpx.Response(404, text="not found")),
        ocr_client=ocr,
    )

    assert await backend.get_ocr_rendition("OBJ1") is None
    # Der Fehlerkoerper darf nicht als Datei in die OCR laufen.
    assert ocr.calls == []


async def test_get_ocr_rendition_returns_none_on_ocr_error(make_backend):
    ocr = _StubOCR(error=OCRUnavailable("Modell nicht erreichbar"))
    backend = make_backend(_handler([]), ocr_client=ocr)

    assert await backend.get_ocr_rendition("OBJ1") is None


async def test_get_ocr_rendition_propagates_session_auth_failure(make_backend):
    ocr = _StubOCR()
    backend = make_backend(
        _handler([], file_response=httpx.Response(401)),
        auth_mode="session",
        ocr_client=ocr,
    )

    # Eine abgelaufene SessionID ist kein OCR-Problem und darf nicht still im
    # Rueckfall verschwinden.
    with pytest.raises(HTTPException) as error:
        await backend.get_ocr_rendition("OBJ1", session_id="OLD-SESSION")

    assert error.value.status_code == 401
    assert ocr.calls == []


# ----------------------------------------------------------------------
# Weiche in get_document
# ----------------------------------------------------------------------


async def test_get_document_uses_ocr_and_skips_rendition(make_backend):
    paths = []
    ocr = _StubOCR()
    backend = make_backend(_handler(paths), ocr_client=ocr)

    document = await backend.get_document("2024-42", "text")

    assert document["content"] == ocr.text
    assert document["mime_type"] == "text/plain"
    assert document["filename"] is None
    assert RENDITION_PATH not in paths


async def test_get_document_falls_back_to_rendition_when_ocr_fails(make_backend):
    paths = []
    ocr = _StubOCR(error=OCRUnavailable("kaputt"))
    backend = make_backend(_handler(paths), ocr_client=ocr)

    document = await backend.get_document("2024-42", "text")

    assert document["content"] == "Volltext aus Enaio"
    assert FILE_PATH in paths
    assert RENDITION_PATH in paths


async def test_get_document_falls_back_for_unsupported_mime_type(make_backend):
    paths = []
    ocr = _StubOCR()
    backend = make_backend(
        _handler(
            paths,
            file_response=httpx.Response(
                200,
                content=b"DOCX",
                headers={
                    "Content-Type": (
                        "application/vnd.openxmlformats-officedocument"
                        ".wordprocessingml.document"
                    )
                },
            ),
        ),
        ocr_client=ocr,
    )

    document = await backend.get_document("2024-42", "text")

    assert document["content"] == "Volltext aus Enaio"
    assert ocr.calls == []


async def test_get_document_without_ocr_client_never_loads_the_file(make_backend):
    paths = []
    backend = make_backend(_handler(paths))

    document = await backend.get_document("2024-42", "text")

    assert document["content"] == "Volltext aus Enaio"
    # Der Default-Pfad darf keinen zusaetzlichen Download ausloesen.
    assert FILE_PATH not in paths


async def test_get_document_vermerk_never_uses_ocr(make_backend):
    paths = []
    ocr = _StubOCR()
    backend = make_backend(
        _handler(paths, object_type_id=VERMERK_OBJECT_TYPE_ID, notiz="Inhalt des Vermerks"),
        ocr_client=ocr,
    )

    document = await backend.get_document("OBJ1", "text")

    # Ein Vermerk hat keine Datei; seine Notiz ist der Inhalt.
    assert document["content"] == "Inhalt des Vermerks"
    assert ocr.calls == []
    assert FILE_PATH not in paths


async def test_get_document_file_format_is_unaffected_by_ocr(make_backend):
    ocr = _StubOCR()
    backend = make_backend(_handler([]), ocr_client=ocr)

    document = await backend.get_document("2024-42", "file")

    assert document["content"] == b"PDFBYTES"
    assert document["mime_type"] == "application/pdf"
    assert ocr.calls == []


# ----------------------------------------------------------------------
# Laengenbegrenzung fuer beide Quellen
# ----------------------------------------------------------------------


async def test_get_text_rendition_truncates_ocr_text(make_backend):
    ocr = _StubOCR(text="A" * 100)
    backend = make_backend(_handler([]), ocr_client=ocr, fulltext_max_chars=10)

    assert await backend.get_text_rendition("OBJ1") == "A" * 10 + TRUNCATION_MARKER


async def test_get_text_rendition_truncates_enaio_rendition(make_backend):
    backend = make_backend(_handler([], rendition_text="B" * 100), fulltext_max_chars=10)

    assert await backend.get_text_rendition("OBJ1") == "B" * 10 + TRUNCATION_MARKER


async def test_get_text_rendition_returns_none_without_any_source(make_backend):
    def handler(request):
        return httpx.Response(404)

    backend = make_backend(handler)

    assert await backend.get_text_rendition("OBJ1") is None
