"""Tests fuer das Tool ``create_case_document``."""

import pytest
from fastapi import HTTPException
from pydantic import TypeAdapter
from pydantic import ValidationError

import EnaioMCP
from rate_limiter import RateLimiter


class _Ctx:
    """Minimaler Ersatz fuer den FastMCP-Context (nur ``info`` wird genutzt)."""

    def __init__(self):
        self.messages = []

    async def info(self, message):
        self.messages.append(message)


@pytest.fixture
def stubbed_document(monkeypatch, tmp_path):
    """Haengt Vorlage, Rendering und Upload so ab, dass kein Enaio noetig ist."""

    monkeypatch.setattr(EnaioMCP, "ASSETS_DIR", tmp_path)
    monkeypatch.setattr(EnaioMCP, "OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(EnaioMCP, "OFFICE_WEB_URL", "https://enaio.test")
    monkeypatch.setattr(EnaioMCP, "upload_limiter", RateLimiter(10))
    (tmp_path / "Vorlage_Vermerk.docx").write_bytes(b"PK")

    def fake_render(template_path, content, out_path, betreff, fields):
        # Datei wirklich anlegen, damit _discard_temp_file sie loeschen kann.
        out_path.write_bytes(b"PK")
        return out_path

    monkeypatch.setattr(EnaioMCP, "_render_document", fake_render)

    def set_object_id(object_id):
        async def fake_upload(
            reference,
            file_path,
            document_type,
            betreff,
            filename,
            session_id=None,
        ):
            assert session_id == "SESSION-1"
            return {"objectId": object_id, "reference_nr": reference}

        monkeypatch.setattr(EnaioMCP.backend, "upload_document", fake_upload)

    return set_object_id


async def test_create_case_document_returns_edit_link(stubbed_document):
    stubbed_document("305821")

    result = await EnaioMCP.create_case_document_session(
        "DS.1.2-2024-1234",
        "Vermerk",
        [{"type": "para", "text": "Inhalt"}],
        "SESSION-1",
        _Ctx(),
    )

    assert result["enaio_object_id"] == "305821"
    assert (
        result["edit_link"]
        == "https://enaio.test/office/desktop/edit/edit/262146/305821"
    )


async def test_create_case_document_without_link_when_object_id_missing(stubbed_document):
    # Ohne ObjectID aus Enaio bleibt das Feld weg statt kaputt zu sein.
    stubbed_document(None)

    result = await EnaioMCP.create_case_document_session(
        "DS.1.2-2024-1234",
        "Vermerk",
        [{"type": "para", "text": "Inhalt"}],
        "SESSION-1",
        _Ctx(),
    )

    assert result["enaio_object_id"] is None
    assert "edit_link" not in result


async def test_get_document_fields_returns_brief_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(EnaioMCP, "ASSETS_DIR", tmp_path)
    (tmp_path / "Vorlage_Brief.docx").write_bytes(b"PK")

    result = await EnaioMCP.get_document_fields("Brief")

    assert result == {
        "document_type": "Brief",
        "template": "Vorlage_Brief.docx",
        "fields": [
            {
                "name": "Adressat",
                "description": (
                    "Name bzw. Bezeichnung der adressierten Person, Stelle oder "
                    "Organisation; nur mit bekannten Angaben befuellen."
                ),
            }
        ],
    }


async def test_get_document_fields_returns_empty_vermerk_fields(monkeypatch, tmp_path):
    monkeypatch.setattr(EnaioMCP, "ASSETS_DIR", tmp_path)
    (tmp_path / "Vorlage_Vermerk.docx").write_bytes(b"PK")

    result = await EnaioMCP.get_document_fields("Vermerk")

    assert result == {
        "document_type": "Vermerk",
        "template": "Vorlage_Vermerk.docx",
        "fields": [],
    }


async def test_get_document_fields_resolves_document_type_case_insensitive(monkeypatch, tmp_path):
    monkeypatch.setattr(EnaioMCP, "ASSETS_DIR", tmp_path)
    (tmp_path / "Vorlage_Brief.docx").write_bytes(b"PK")

    result = await EnaioMCP.get_document_fields("  BRIEF ")

    assert result["document_type"] == "Brief"
    assert result["template"] == "Vorlage_Brief.docx"
    assert result["fields"][0]["name"] == "Adressat"


async def test_get_document_fields_unknown_type_raises_400():
    with pytest.raises(HTTPException) as excinfo:
        await EnaioMCP.get_document_fields("Gutachten")

    assert excinfo.value.status_code == 400
    assert "Unbekannter Dokumententyp" in excinfo.value.detail


async def test_get_document_fields_is_marked_read_only():
    tool = await EnaioMCP.mcp.get_tool("get_document_fields")

    assert tool.annotations.readOnlyHint is True
    assert tool.annotations.destructiveHint is False
    assert tool.annotations.idempotentHint is True


async def test_guardrail_reaches_the_client():
    # FastMCP schneidet den Docstring am :param-Block ab. Stand die Aufrufregel dahinter,
    # kam sie beim Modell nie an - deshalb hier gegen die tatsaechlich ausgelieferte
    # Tool-Beschreibung pruefen, nicht gegen __doc__.
    tool = await EnaioMCP.mcp.get_tool("create_case_document")
    description = tool.description

    for marker in (
        "OPTIONALE PARAMETER",
        "betreff ist ein optionaler Betreff",
        "fields ist ein optionales JSON-Objekt",
        "PFLICHT VOR DEM AUFRUF",
        "get_document_fields",
        "muss get_document_fields",
        "darf create_case_document nicht",
        "relevanten Platzhalter",
        "KERNREGEL",
        "ZULÄSSIGKEITSPRÜFUNG",
        "WAS ALS AUSDRÜCKLICHE SPEICHERANWEISUNG GILT",
        "WAS AUSDRÜCKLICH KEINE SPEICHERANWEISUNG IST",
        "VERBOT IMPLIZITER ANNAHMEN",
        "FAIL-CLOSED-REGEL",
        "MERKSATZ",
    ):
        assert marker in description

    # Der Merksatz muss der Schluss der Beschreibung bleiben (Recency), der :param-Block
    # wird von FastMCP entfernt.
    assert description.rstrip().endswith("in Enaio zu speichern.")
    assert ":param" not in description


async def test_fields_annotation_mentions_common_placeholders():
    tool = await EnaioMCP.mcp.get_tool("create_case_document")
    description = tool.parameters["properties"]["fields"]["description"]

    for placeholder in (
        "Adressat",
        "PLZ",
        "Ort",
        "Ansprechpartner",
        "Abteilung",
        "Anschrift",
    ):
        assert placeholder in description
        assert f"[{placeholder}]" in description

    for marker in (
        "sämtliche bekannten Angaben",
        "Name",
        "Bearbeiter",
        "Organisation",
        "Postleitzahl",
        "ausschließlich",
        "nicht zu ergänzen",
        "nicht zu fingieren",
        "get_document_fields",
        "Pflicht vor create_case_document",
        "Ohne vorherigen get_document_fields-Aufruf",
        "darf create_case_document nicht",
        "relevanten",
        "document_type",
    ):
        assert marker in description


async def test_optional_document_parameter_annotations_reach_schema():
    tool = await EnaioMCP.mcp.get_tool("create_case_document")
    properties = tool.parameters["properties"]

    assert properties["betreff"]["default"] is None
    assert "Optionaler Betreff" in properties["betreff"]["description"]
    assert "Betreffzeile" in properties["betreff"]["description"]

    assert properties["fields"]["default"] is None
    assert "Optionale Zuordnung" in properties["fields"]["description"]
    assert "JSON-Objekt" in properties["fields"]["description"]


async def test_fields_annotation_rejects_json_object_as_string():
    tool = await EnaioMCP.mcp.get_tool("create_case_document")
    description = tool.parameters["properties"]["fields"]["description"]

    assert "echtes JSON-Objekt" in description
    assert "nicht als String" in description
    assert "json.dumps" in description
    assert '{"Adressat":"Ministerium für Bildung","PLZ":"12345","Ort":"Musterstadt"}' in description
    assert '{\\"Adressat\\":\\"Ministerium für Bildung\\",\\"PLZ\\":\\"12345\\",\\"Ort\\":\\"Musterstadt\\"}' in description


def test_fields_parameter_accepts_json_object_string_before_tool_call_validation():
    adapter = TypeAdapter(EnaioMCP.CreateCaseDocumentFields)

    fields = adapter.validate_python(
        '{"Adressat": "Frau Janina Lubicz", "Abteilung": "Referat D 4", '
        '"Ansprechpartner": "Janina Lubicz", '
        '"Anschrift": "Franz-Josef-Roder-Strasse 17", "PLZ": "66119", '
        '"Ort": "Saarbruecken", "Organisation": "MWIDE"}'
    )

    assert fields == {
        "Adressat": "Frau Janina Lubicz",
        "Abteilung": "Referat D 4",
        "Ansprechpartner": "Janina Lubicz",
        "Anschrift": "Franz-Josef-Roder-Strasse 17",
        "PLZ": "66119",
        "Ort": "Saarbruecken",
        "Organisation": "MWIDE",
    }


def test_fields_parameter_rejects_json_array_string_before_tool_call_validation():
    adapter = TypeAdapter(EnaioMCP.CreateCaseDocumentFields)

    with pytest.raises(ValidationError, match="fields muss ein JSON-Objekt sein"):
        adapter.validate_python('[{"Adressat": "Frau Janina Lubicz"}]')


async def test_content_annotation_rejects_json_array_as_string():
    tool = await EnaioMCP.mcp.get_tool("create_case_document")
    description = tool.parameters["properties"]["content"]["description"]

    assert "JSON-Array" in description
    assert "nicht als String" in description
    assert "json.dumps" in description
    assert '{"content":[{"type":"para","text":"Text"}]}' in description
    assert '{"content":"[{\\"type\\":\\"para\\",\\"text\\":\\"Text\\"}]"}' in description


def test_content_parameter_accepts_json_array_string_before_tool_call_validation():
    adapter = TypeAdapter(EnaioMCP.CreateCaseDocumentContent)

    content = adapter.validate_python(
        '[{"type": "heading", "text": "1. Sachverhalt"}, '
        '{"type": "para", "text": "Inhalt"}]'
    )

    assert content == [
        {"type": "heading", "text": "1. Sachverhalt"},
        {"type": "para", "text": "Inhalt"},
    ]


def test_content_parameter_rejects_json_object_string_before_tool_call_validation():
    adapter = TypeAdapter(EnaioMCP.CreateCaseDocumentContent)

    with pytest.raises(ValidationError, match="content muss ein JSON-Array sein"):
        adapter.validate_python('{"type": "para", "text": "Inhalt"}')


async def test_content_annotation_forbids_repeating_subject_and_reference():
    tool = await EnaioMCP.mcp.get_tool("create_case_document")
    description = tool.parameters["properties"]["content"]["description"]

    assert "Betreff" in description
    assert "Aktenzeichen" in description
    assert "nicht wiederholt" in description
    assert "betreff und reference" in description


async def test_create_case_document_is_marked_destructive():
    # Clients, die auf destructiveHint reagieren, sollen vor dem Speichern nachfragen.
    tool = await EnaioMCP.mcp.get_tool("create_case_document")

    assert tool.annotations.destructiveHint is True
    assert tool.annotations.readOnlyHint is False


async def test_server_instructions_mention_the_save_rule():
    instructions = EnaioMCP.mcp.instructions

    assert "create_case_document" in instructions
    assert "Speicheranweisung" in instructions
    assert "get_document_fields" in instructions
    assert "ohne diesen vorherigen Aufruf darf create_case_document nicht" in instructions
    if EnaioMCP.AUTH_MODE == EnaioMCP.AUTH_MODE_SESSION:
        assert "SessionID" in instructions
    else:
        assert "SessionID" not in instructions
