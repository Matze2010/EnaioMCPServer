"""Tests fuer das Tool ``create_case_document``."""

import pytest

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
    assert '{"fields":{"Adressat":"Ministerium für Bildung","PLZ":"12345","Ort":"Musterstadt"}}' in description
    assert '{"fields":"{\\"Adressat\\":\\"Ministerium für Bildung\\",\\"PLZ\\":\\"12345\\",\\"Ort\\":\\"Musterstadt\\"}"}' in description


async def test_content_annotation_rejects_json_array_as_string():
    tool = await EnaioMCP.mcp.get_tool("create_case_document")
    description = tool.parameters["properties"]["content"]["description"]

    assert "JSON-Array" in description
    assert "nicht als String" in description
    assert "json.dumps" in description
    assert '{"content":[{"type":"para","text":"Text"}]}' in description
    assert '{"content":"[{\\"type\\":\\"para\\",\\"text\\":\\"Text\\"}]"}' in description


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
    if EnaioMCP.AUTH_MODE == EnaioMCP.AUTH_MODE_SESSION:
        assert "SessionID" in instructions
    else:
        assert "SessionID" not in instructions
