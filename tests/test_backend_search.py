"""Tests fuer die zentrale Such- und Fehlerbehandlung in EnaioBackend."""

import json

import httpx
import pytest
from fastapi import HTTPException

from EnaioBackend import OBJECT_TYPES, UPLOAD_OBJECT_TYPE_ID


def _akte_object():
    return {
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


def _running_case_object(
    object_id, aktenzeichen, title="Titel", topics="A|B", creation_date="2024-01-01"
):
    return {
        "properties": {
            "system:objectId": {"value": object_id},
            "system:creationDate": {"value": creation_date},
            "Aktenzeichen": {"value": aktenzeichen},
            "Aktenbezeichnung": {"value": title},
            "Kategorisierung": {"value": "Standard"},
            "Aktenplaneintrag": {"value": topics},
            "Aktenstatus": {"value": "laufend"},
            "Aktentyp": {"value": "Standardakte"},
        }
    }


def _child_object(identifier, title):
    return {
        "properties": {
            "documentIdentifier": {"value": identifier},
            "documentTitle": {"value": title},
            "system:creationDate": {"value": "2024-01-01"},
            "system:lastModificationDate": {"value": "2024-01-02"},
        }
    }


def _empty(_request):
    return httpx.Response(200, json={"objects": []})


async def test_get_aktenzeichen_returns_record(make_backend):
    backend = make_backend(lambda request: httpx.Response(200, json={"objects": [_akte_object()]}))

    object_id, record = await backend.get_aktenzeichen("DS.1.2-2024-1234")

    assert object_id == "PARENT123"
    assert record["reference_nr"] == "DS.1.2-2024-1234"
    assert record["title"] == "Titel"
    assert record["topics"] == ["A", "B"]
    assert record["creationDate"] == "2024-01-01"


async def test_search_sends_expected_query_envelope(make_backend):
    captured = {}

    def handler(request):
        captured["body"] = request.content
        return httpx.Response(200, json={"objects": [_akte_object()]})

    backend = make_backend(handler)
    await backend.get_aktenzeichen("DS.1.2-2024-1234")

    query = json.loads(captured["body"])["query"]
    assert query["parameters"] == {
        "aktenzeichen": "DS.1.2-2024-1234",
        "aktentyp": "Standardakte",
    }
    assert "Aktentyp=@aktentyp" in query["statement"]
    assert "system:creationDate" in query["statement"]
    assert query["skipCount"] == 0
    assert query["handleDeletedDocuments"] == "DELETED_DOCUMENTS_EXCLUDE"
    assert query["options"] == {"Rights": 0, "RegisterContext": 0}
    assert "limit" not in query


async def test_get_running_cases_sends_expected_query(make_backend):
    captured = {}

    def handler(request):
        captured["body"] = request.content
        return httpx.Response(200, json={"objects": []})

    backend = make_backend(handler)
    await backend.get_running_cases("gisch")

    query = json.loads(captured["body"])["query"]
    assert query["parameters"] == {
        "user": "gisch",
        "status": "laufend",
        "aktentyp": "Standardakte",
    }
    assert query["handleDeletedDocuments"] == "DELETED_DOCUMENTS_EXCLUDE"
    assert query["options"] == {"Rights": 0, "RegisterContext": 0}

    statement = query["statement"]
    assert "system:creationDate" in statement
    assert "FROM OSTPL_AA " in statement
    assert "Aktenverantwortlicher=@user" in statement
    assert "Aktenstatus=@status" in statement
    assert "Aktentyp=@aktentyp" in statement
    # Der Akteninhalt wird bewusst nicht mitgelesen (kompakte Liste).
    assert "Akteninhalt" not in statement


async def test_get_running_cases_maps_records(make_backend):
    objects = [
        _running_case_object(
            "15645",
            "DS.5.1-2022-577",
            "Erster Vorgang",
            "Datenschutz|OWi",
            creation_date="2022-03-04",
        ),
        _running_case_object("17776", "DS.7.2-2022-695", "Zweiter Vorgang"),
    ]
    backend = make_backend(lambda request: httpx.Response(200, json={"objects": objects}))

    cases = await backend.get_running_cases("gisch")

    assert cases == [
        {
            "reference_nr": "DS.5.1-2022-577",
            "title": "Erster Vorgang",
            "category": "Standard",
            "creationDate": "2022-03-04",
            "topics": ["Datenschutz", "OWi"],
            "status": "laufend",
            "object_id": "15645",
        },
        {
            "reference_nr": "DS.7.2-2022-695",
            "title": "Zweiter Vorgang",
            "category": "Standard",
            "creationDate": "2024-01-01",
            "topics": ["A", "B"],
            "status": "laufend",
            "object_id": "17776",
        },
    ]


async def test_get_running_cases_empty_returns_empty_list(make_backend):
    """Keine Treffer ist bei einer Auflistung kein Fehler (kein HTTP 404)."""
    backend = make_backend(_empty)

    assert await backend.get_running_cases("niemand") == []


async def test_get_document_query_options_differ(make_backend):
    """get_document nutzt FileInfo/Baseparams, limit=1 und kein handleDeletedDocuments."""
    captured = {}

    def handler(request):
        captured["body"] = request.content
        return httpx.Response(
            200,
            json={
                "objects": [
                    {
                        "properties": {
                            "system:objectTypeId": {"value": "262144"},
                            "system:objectId": {"value": "V1"},
                            "OSTPL_AA_AN_CONTACTMEDIA": {"value": "Vermerktitel"},
                            "OSTPL_AA_AN_NOTIZ": {"value": "Inhalt des Vermerks"},
                            "system:creationDate": {"value": "2024-01-01"},
                            "system:lastModificationDate": {"value": "2024-01-02"},
                        }
                    }
                ]
            },
        )

    backend = make_backend(handler)
    await backend.get_document("V1", "text")

    query = json.loads(captured["body"])["query"]
    assert query["limit"] == 1
    assert "handleDeletedDocuments" not in query
    assert query["options"] == {
        "Rights": 0,
        "Baseparams": 1,
        "RegisterContext": 0,
        "FileInfo": 1,
    }


async def test_get_document_vermerk_uses_notiz_without_content_request(make_backend):
    requests_seen = []

    def handler(request):
        requests_seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "objects": [
                    {
                        "properties": {
                            "system:objectTypeId": {"value": "262144"},
                            "system:objectId": {"value": "V1"},
                            "OSTPL_AA_AN_CONTACTMEDIA": {"value": "Vermerktitel"},
                            "OSTPL_AA_AN_NOTIZ": {"value": "Inhalt des Vermerks"},
                            "system:creationDate": {"value": "2024-01-01"},
                            "system:lastModificationDate": {"value": "2024-01-02"},
                        }
                    }
                ]
            },
        )

    backend = make_backend(handler)
    document = await backend.get_document("V1", "text")

    assert document == {
        "type": "vermerk",
        "document_nr": "V1",
        "name": "Vermerktitel",
        "creationDate": "2024-01-01",
        "lastModificationDate": "2024-01-02",
        "content": "Inhalt des Vermerks",
    }
    # Nur die Suche, kein zusaetzlicher Content-Abruf.
    assert requests_seen == ["/api/dms/objects/search"]


async def test_get_document_file_fetches_content(make_backend):
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
        assert request.url.path == "/api/dms/objects/OBJ1/contents/file/1"
        return httpx.Response(200, content=b"BINARY")

    backend = make_backend(handler)
    document = await backend.get_document("2024-42", "file")

    assert document["type"] == "file"
    assert document["document_nr"] == "2024-42"
    assert document["content"] == b"BINARY"


async def test_get_rendition_returns_none_on_error_status(make_backend):
    def handler(request):
        return httpx.Response(404)

    backend = make_backend(handler)
    assert await backend.get_rendition("OBJ1") is None


async def test_get_document_list_queries_all_object_types(make_backend):
    tables = []

    def handler(request):
        statement = json.loads(request.content)["query"]["statement"]
        table = statement.split(" FROM ")[1].split(" ")[0]
        tables.append(table)
        # Nur der erste Typ liefert einen Treffer.
        if table == "OSTPL_AA_DOKUMENT":
            return httpx.Response(200, json={"objects": [_child_object("2024-1", "Erstes")]})
        return httpx.Response(200, json={"objects": []})

    backend = make_backend(handler)
    documents = await backend.get_document_list("PARENT123")

    assert tables == [t.table for t in OBJECT_TYPES.values()]
    assert documents == [
        {
            "type": "file",
            "id": "2024-1",
            "name": "Erstes",
            "creationDate": "2024-01-01",
            "lastModificationDate": "2024-01-02",
        }
    ]


async def test_connection_error_maps_to_503(make_backend):
    def handler(request):
        raise httpx.ConnectError("connection refused", request=request)

    backend = make_backend(handler)
    with pytest.raises(HTTPException) as excinfo:
        await backend.get_aktenzeichen("DS.1.2-2024-1234")

    assert excinfo.value.status_code == 503
    assert "ENAIO API" in excinfo.value.detail


async def test_unexpected_status_maps_to_502(make_backend):
    backend = make_backend(lambda request: httpx.Response(500, text="boom"))

    with pytest.raises(HTTPException) as excinfo:
        await backend.get_aktenzeichen("DS.1.2-2024-1234")

    assert excinfo.value.status_code == 502


async def test_malformed_payload_maps_to_500(make_backend):
    backend = make_backend(lambda request: httpx.Response(200, json={"unerwartet": 1}))

    with pytest.raises(HTTPException) as excinfo:
        await backend.get_aktenzeichen("DS.1.2-2024-1234")

    assert excinfo.value.status_code == 500


async def test_empty_result_maps_to_404(make_backend):
    backend = make_backend(_empty)

    with pytest.raises(HTTPException) as excinfo:
        await backend.get_aktenzeichen("DS.1.2-2024-9999")
    assert excinfo.value.status_code == 404
    assert "DS.1.2-2024-9999" in excinfo.value.detail


async def test_empty_document_result_maps_to_404(make_backend):
    backend = make_backend(_empty)

    with pytest.raises(HTTPException) as excinfo:
        await backend.get_document("UNBEKANNT", "text")
    assert excinfo.value.status_code == 404
    assert "UNBEKANNT" in excinfo.value.detail
